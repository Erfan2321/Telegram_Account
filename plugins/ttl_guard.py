"""
TTL Media Guard — permanent storage for self-destructing / view-once media.

Mechanism:
    Telegram delivers the file to the client like any other one; `ttl_seconds`
    on the raw media object is the only marker, and destruction is the official
    client's job rather than the server's.

    What decides whether this works is timing. Per core.telegram.org/api/views
    the client calls `messages.readMessageContents` when the user opens the
    media, and *that* call starts the timer — after it the file reference stops
    working. So this module never calls it: it downloads on arrival, and the
    media stays "unviewed" from the sender's side.

    One field covers every timed kind, and all of them are handled here: photo,
    video, video note, voice, animation, audio and plain document — whether the
    sender picked a countdown (`0 < ttl < 0x7FFFFFFF`) or "view once"
    (`ttl == 0x7FFFFFFF`).

Why a raw handler:
    `Message._parse` passes `ttl_seconds` through for photos (message.py:670)
    and non-round videos (:712) and drops it for voice, video notes, animations
    and documents. From `on_message` a one-time voice is indistinguishable from
    an ordinary one. On the raw update `ttl_seconds` sits on the media object
    itself and every kind looks the same.

Why the download does not go through `Message._parse` either:
    The parsed objects throw away exactly what a faithful re-upload needs — a
    `VideoNote` keeps no `ttl_seconds`, and Pyrogram's senders default duration
    and dimensions to zero, so a re-sent video note becomes a 1x1 pixel,
    0-second circle. `Photo._parse` also raises IndexError on a photo carrying
    no downloadable size (§11.4). The file id is therefore built straight from
    the raw photo/document.

Why this module reports its own failures:
    A rescue cannot be retried later — by the time anyone reads a log the media
    is gone. And the library hides the reason: `Client.get_file` catches its own
    exceptions and merely logs them (client.py:1019), so a failed download
    arrives here as an empty buffer with nothing attached. So the reason is
    captured from the library's logger, three independent download routes are
    tried, and if all of them fail a Persian report is sent to Saved Messages
    naming the cause. Silence was the actual bug: media that never arrived
    looked exactly like media that was never sent.

Scope:
    Incoming private-chat messages only.
"""

import asyncio
import html
import io
import logging
import re
import time
from collections import deque
from typing import NamedTuple

from pyrogram import Client, filters, raw, types
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait
from pyrogram.file_id import FileId, FileType, ThumbnailSource

import config
from utils import paths
from utils.helpers import (
    contact_lines, flood_seconds, respond, safe_handler, time_text,
)
from utils.media import named_buffer

log = logging.getLogger(__name__)


# --- Dispatcher group ---
# Priority is a negative integer (-1).
# In Pyrogram's dispatcher loop (`pyrogram/dispatcher.py`) execution breaks
# after the first matching handler *within a group*, and a raw handler matches
# every update unconditionally. In group 0 it would swallow every update and
# silently disable commands, the anti-delete logger and auto-seen.
#
# Groups are iterated in ascending order, so a negative group also runs this
# handler before group 0 — which matters, because auto_seen's
# `read_chat_history` lives there and nothing that touches the chat may run
# before the download.
TTL_GROUP = -1

# Telegram marks "view once" with the magic integer 0x7FFFFFFF (INT32_MAX)
# instead of a realistic countdown.
VIEW_ONCE_TTL = 0x7FFFFFFF

# --- Deduplication cache ---
# Stops a re-delivered update (reconnect, sequence gap) from saving twice.
# Schema: {(chat_id, message_id): monotonic timestamp}
_PROCESSED = {}
_PROCESSED_TTL = 600

# Runtime counters for the status command.
#   saved  — landed in Saved Messages
#   failed — download or upload raised
#   lost   — the payload was already gone by the time we asked for it
_STATS = {"saved": 0, "failed": 0, "lost": 0}

# Last few outcomes, shown by `.ttl`. The whole point is that a rescue happens
# once and cannot be repeated, so the outcome has to survive somewhere the user
# can actually read it without a terminal.
_LAST_EVENTS = deque(maxlen=8)

# Longest FloodWait we are willing to sleep through before giving up.
FLOOD_WAIT_LIMIT = 60

# Telegram's caption limit for a media message (a text message allows 4096).
CAPTION_LIMIT = 1024

# Room kept free in a caption for a note about a fallback.
NOTE_ROOM = 160

# Fallback edge length for a video note whose attribute says 0. Telegram's own
# round videos are 384 or 640 px; Pyrogram's default of 1 renders as a dot.
DEFAULT_NOTE_LENGTH = 384

# How much of the raw media structure goes into a debug report.
DUMP_LIMIT = 2500

# upload.getFile chunking. 1 MB is the protocol maximum per request; the limit
# also has to be a multiple of 4096, which it is.
CHUNK_SIZE = 1024 * 1024

# Above this the fast route hands over to Pyrogram's own downloader: a large
# transfer over the main session would compete with update delivery, and the
# race the fast route exists to win is already lost on a file that big.
MAX_FAST_BYTES = 32 * 1024 * 1024

# Persian labels for the report; keys are our internal media kinds.
_KIND_LABELS = {
    "photo": "عکس",
    "video": "ویدیو",
    "video_note": "ویدیو مسیج",
    "voice": "ویس",
    "animation": "گیف",
    "audio": "فایل صوتی",
    "document": "فایل",
}

# Extension used when the mime type maps to nothing.
_EXT_FALLBACK = {
    "photo": ".jpg",
    "video": ".mp4",
    "video_note": ".mp4",
    "animation": ".mp4",
    "voice": ".ogg",
    "audio": ".mp3",
    "document": ".bin",
}

# Everything outside this set is replaced in a sender-supplied file name.
_UNSAFE_NAME_CHARS = re.compile(r"[^\w.\-() ؀-ۿ]+")


class _Media(NamedTuple):
    """What is needed to fetch the payload and re-upload it faithfully."""

    kind: str
    file_id: str
    location: object
    dc_id: int
    file_name: str
    file_size: int
    duration: int = 0
    width: int = 0
    height: int = 0
    performer: str = ""
    title: str = ""
    supports_streaming: bool = False


def _seen_before(key) -> bool:
    """Report whether this message was already handled, and record it."""
    now = time.monotonic()

    # Prune expired entries on write, so the dict cannot grow forever.
    for k, ts in list(_PROCESSED.items()):
        if now - ts > _PROCESSED_TTL:
            _PROCESSED.pop(k, None)

    if key in _PROCESSED:
        return True

    _PROCESSED[key] = now
    return False


def _record_event(kind: str, outcome: str, detail: str = "") -> None:
    """Remember an outcome for `.ttl`."""
    _LAST_EVENTS.appendleft((
        time_text(fmt="%H:%M:%S"),
        _KIND_LABELS.get(kind, kind or "?"),
        outcome,
        detail[:160],
    ))


def _describe_ttl(ttl: int) -> str:
    """Human-readable form of the timer."""
    if ttl >= VIEW_ONCE_TTL:
        return "فقط یک بار نمایش (view once)"
    return f"{ttl} ثانیه"


def _dump(obj) -> str:
    """Readable form of a raw TL object, for the debug report."""
    try:
        text = str(obj)
    except Exception:
        text = repr(obj)
    return text[:DUMP_LIMIT]


class _CapturedLibraryError:
    """
    Catch the exception Pyrogram logs instead of raising.

    `Client.get_file` ends with `except Exception as e: log.exception(e)`
    (client.py:1019), so the download simply stops and `handle_download` returns
    an empty buffer. The exception exists exactly once, on the `pyrogram.client`
    logger, and this is the only place it can be read from. A temporary handler
    on that logger is enough; nothing about the library is modified.
    """

    def __init__(self):
        self.detail = ""
        self._logger = logging.getLogger("pyrogram.client")
        self._handler = None

    def __enter__(self):
        captor = self

        class _Grab(logging.Handler):
            def emit(self, record):
                if captor.detail or not record.exc_info:
                    return
                error = record.exc_info[1]
                captor.detail = f"{type(error).__name__}: {error}"

        self._handler = _Grab()
        self._logger.addHandler(self._handler)
        return self

    def __exit__(self, *exc_info):
        if self._handler is not None:
            self._logger.removeHandler(self._handler)
        return False


def _safe_name(name: str) -> str:
    """
    Make a sender-supplied file name safe to use as a file name.

    `DocumentAttributeFilename` is arbitrary text chosen by the other party and
    it ends up both on the local copy and in the re-upload, so directory
    separators and leading dots have to go: a name like `a/../../../evil` would
    otherwise walk out of data/ttl_media/.
    """
    name = (name or "").replace("\\", "/").split("/")[-1].strip()
    name = _UNSAFE_NAME_CHARS.sub("_", name).lstrip(".")
    return name[:80]


def _upload_name(client, kind: str, given: str, mime: str) -> str:
    """
    Name for the in-memory buffer.

    The senders re-guess the mime type from this name — `send_voice` does
    `guess_mime_type(voice.name) or "audio/mpeg"` — so an extension that
    disagrees with the payload uploads an OGG labelled as an MP3. The sender's
    own name is kept only when it agrees with the document's mime type.
    """
    safe = _safe_name(given)
    if safe and mime and client.guess_mime_type(safe) == mime:
        return safe

    extension = (client.guess_extension(mime) if mime else None) or _EXT_FALLBACK[kind]
    return f"{kind}_{time_text(fmt='%Y%m%d-%H%M%S')}{extension}"


def _largest_photo_size(photo):
    """
    The biggest downloadable size of a photo, as `(type, byte size)`.

    Only `PhotoSize` and `PhotoSizeProgressive` can be fetched with
    `upload.getFile`; a stripped or cached preview is inline data with no
    server-side counterpart. A photo carrying nothing else is not downloadable
    at all, and the caller has to re-read the message instead.
    """
    best = None

    for size in getattr(photo, "sizes", None) or []:
        if isinstance(size, raw.types.PhotoSize):
            candidate = (size.type, size.size)
        elif isinstance(size, raw.types.PhotoSizeProgressive):
            candidate = (size.type, max(size.sizes or [0]))
        else:
            continue

        if best is None or candidate[1] > best[1]:
            best = candidate

    return best


def _photo_media(client, photo) -> "_Media | None":
    """Describe a raw Photo. None means there is nothing to download."""
    if not isinstance(photo, raw.types.Photo):
        return None

    best = _largest_photo_size(photo)
    if best is None:
        return None

    file_id = FileId(
        file_type=FileType.PHOTO,
        dc_id=photo.dc_id,
        media_id=photo.id,
        access_hash=photo.access_hash,
        file_reference=photo.file_reference,
        thumbnail_source=ThumbnailSource.THUMBNAIL,
        thumbnail_file_type=FileType.PHOTO,
        thumbnail_size=best[0],
        volume_id=0,
        local_id=0,
    ).encode()

    return _Media(
        kind="photo",
        file_id=file_id,
        location=raw.types.InputPhotoFileLocation(
            id=photo.id,
            access_hash=photo.access_hash,
            file_reference=photo.file_reference,
            thumb_size=best[0],
        ),
        dc_id=photo.dc_id,
        file_name=_upload_name(client, "photo", "", "image/jpeg"),
        file_size=best[1],
    )


def _document_media(client, doc) -> "_Media | None":
    """Describe a raw Document. None means there is nothing to download."""
    if not isinstance(doc, raw.types.Document):
        return None

    attributes = {type(a): a for a in doc.attributes or []}
    video = attributes.get(raw.types.DocumentAttributeVideo)
    audio = attributes.get(raw.types.DocumentAttributeAudio)

    # Same order of checks as Message._parse: an animation carries a video
    # attribute as well, so DocumentAttributeAnimated has to be tested first,
    # and a round video is a video note rather than a video.
    if raw.types.DocumentAttributeAnimated in attributes:
        kind, file_type = "animation", FileType.ANIMATION
    elif video is not None and video.round_message:
        kind, file_type = "video_note", FileType.VIDEO_NOTE
    elif video is not None:
        kind, file_type = "video", FileType.VIDEO
    elif audio is not None and audio.voice:
        kind, file_type = "voice", FileType.VOICE
    elif audio is not None:
        kind, file_type = "audio", FileType.AUDIO
    else:
        kind, file_type = "document", FileType.DOCUMENT

    given_name = getattr(attributes.get(raw.types.DocumentAttributeFilename), "file_name", "") or ""

    file_id = FileId(
        file_type=file_type,
        dc_id=doc.dc_id,
        media_id=doc.id,
        access_hash=doc.access_hash,
        file_reference=doc.file_reference,
    ).encode()

    return _Media(
        kind=kind,
        file_id=file_id,
        location=raw.types.InputDocumentFileLocation(
            id=doc.id,
            access_hash=doc.access_hash,
            file_reference=doc.file_reference,
            thumb_size="",
        ),
        dc_id=doc.dc_id,
        file_name=_upload_name(client, kind, given_name, doc.mime_type or ""),
        file_size=doc.size or 0,
        duration=getattr(video or audio, "duration", 0) or 0,
        width=getattr(video, "w", 0) or 0,
        height=getattr(video, "h", 0) or 0,
        performer=getattr(audio, "performer", "") or "",
        title=getattr(audio, "title", "") or "",
        supports_streaming=bool(getattr(video, "supports_streaming", False)),
    )


def inspect_media(client, media) -> "_Media | None":
    """Describe raw media of either kind, or None if it carries no payload."""
    if isinstance(media, raw.types.MessageMediaPhoto):
        return _photo_media(client, media.photo)

    if isinstance(media, raw.types.MessageMediaDocument):
        return _document_media(client, media.document)

    return None


def _buffer_size(buf) -> int:
    """Byte count of an in-memory download, leaving the cursor at the start."""
    buf.seek(0, io.SEEK_END)
    size = buf.tell()
    buf.seek(0)
    return size


class _NeedsSlowPath(Exception):
    """The fast route cannot serve this file; Pyrogram's own path must."""


async def _fast_download(client, spec: _Media) -> bytes:
    """
    Fetch the payload over the connection we already have.

    This is the part that decides whether a rescue succeeds at all, and it is
    about latency rather than throughput. `Client.get_file` builds a **brand
    new `Session` for every single download** (client.py:865): TCP connect,
    `Ping`, `InvokeWithLayer(InitConnection(...))` — and, when the file lives on
    another DC, a full `Auth` handshake plus `ExportAuthorization` /
    `ImportAuthorization` — all of it *before the first byte of the file*. On a
    slow or proxied link that is comfortably a second or more.

    That second is the whole game. This handler is racing the account's other
    devices: the moment the phone opens the media it calls
    `messages.readMessageContents` and the server invalidates the file
    reference mid-transfer. `upload.getFile` on the main session starts the
    transfer immediately, which is the difference between rescuing a video note
    and reporting that it vanished.

    It also lets RPC errors through instead of swallowing them, so
    `FILE_REFERENCE_EXPIRED` reads as itself.

    Raises `_NeedsSlowPath` for the two cases Pyrogram's own path handles and
    this one deliberately does not: a file on another DC and a CDN redirect.
    """
    if spec.location is None:
        raise _NeedsSlowPath("no file location")

    if spec.file_size and spec.file_size > MAX_FAST_BYTES:
        raise _NeedsSlowPath(f"{spec.file_size} bytes is over the fast-path limit")

    if spec.dc_id != await client.storage.dc_id():
        # Another DC needs the exported-authorization dance; that is exactly
        # what Pyrogram's path does, so do not reimplement it here.
        raise _NeedsSlowPath(f"file is on DC {spec.dc_id}")

    data = bytearray()
    offset = 0

    while True:
        result = await client.invoke(
            raw.functions.upload.GetFile(
                location=spec.location,
                offset=offset,
                limit=CHUNK_SIZE,
            )
        )

        if not isinstance(result, raw.types.upload.File):
            # upload.FileCdnRedirect — the CDN needs its own session, key and
            # hash verification, all of which Pyrogram already implements.
            raise _NeedsSlowPath(type(result).__name__)

        chunk = result.bytes
        data += chunk
        offset += len(chunk)

        if len(chunk) < CHUNK_SIZE:
            break
        if spec.file_size and offset >= spec.file_size:
            break
        if offset > MAX_FAST_BYTES:
            raise _NeedsSlowPath("file kept growing past the fast-path limit")

    return bytes(data)


async def _download(client, spec: _Media):
    """
    Download the payload into memory. Returns `(buffer | None, reason)`.

    The explicit size check is what makes the None case possible at all:
    `Client.get_file` catches its own exceptions and only logs them
    (client.py:1019), so `handle_download` sees a generator that simply ended
    and hands back an *empty* BytesIO rather than raising. Without this an
    expired file reference would be re-uploaded to Saved Messages as a 0-byte
    file, and the real error would never be seen by anything.
    """
    with _CapturedLibraryError() as captured:
        buf = await client.download_media(
            spec.file_id,
            file_name=spec.file_name,
            in_memory=True,
        )

    if buf is None:
        return None, captured.detail or "download_media returned nothing"

    size = _buffer_size(buf)
    if size == 0 or (spec.file_size and size < spec.file_size):
        buf.close()
        return None, (
            captured.detail
            or f"got {size} of {spec.file_size} bytes"
        )

    return buf, ""


async def _refetch_media(client, message_id: int):
    """
    Re-read the message to obtain a fresh file reference.

    `messages.getMessages` does not mark anything as read, so it is safe here.
    It covers two cases: an update that arrived without a payload at all
    (`photo`/`document` is `flags.0` on both media types and can legitimately
    be absent) and a file reference the server has already invalidated.
    """
    result = await client.invoke(
        raw.functions.messages.GetMessages(id=[raw.types.InputMessageID(id=message_id)])
    )

    for message in getattr(result, "messages", None) or []:
        if isinstance(message, raw.types.Message) and message.id == message_id:
            return message.media

    return None


async def _download_via_parser(client, message, users, chats):
    """
    Last download route: let Pyrogram parse the message and download that.

    It should resolve to the same file id we build ourselves — the headless
    check asserts exactly that — but "should" is not "does", and this costs one
    local parse. If the high-level object is what works, the report says so.
    """
    parsed = await types.Message._parse(client, message, users, chats, replies=0)

    with _CapturedLibraryError() as captured:
        buf = await client.download_media(parsed, in_memory=True)

    if buf is None:
        return None, captured.detail or "download_media returned nothing"

    if _buffer_size(buf) == 0:
        buf.close()
        return None, captured.detail or "got 0 bytes"

    return buf, ""


async def _fetch(client, spec: _Media, label: str, tried: list):
    """
    One media description, both download routes: fast first, library second.

    The library route is not redundant — it is the one that knows how to reach
    another DC and how to decrypt a CDN redirect, and `_fast_download` refuses
    both on purpose.
    """
    try:
        data = await _fast_download(client, spec)
        if data and (not spec.file_size or len(data) >= spec.file_size):
            return named_buffer(data, spec.file_name)
        tried.append(f"{label}/fast (got {len(data)} of {spec.file_size} bytes)")
    except FloodWait:
        raise
    except _NeedsSlowPath as e:
        # Not a failure — a routing decision. It still belongs in the report,
        # because "which route ran" is half of reading one.
        log.info("TTL fast route declined (%s): %s", label, e)
        tried.append(f"{label}/fast declined ({e})")
    except Exception as e:
        # This is the branch the whole fast route was worth writing for: the
        # RPC error arrives intact instead of being logged and dropped.
        tried.append(f"{label}/fast ({type(e).__name__}: {e})")

    buf, why = await _download(client, spec)
    if buf is not None:
        return buf
    tried.append(f"{label}/library ({why})")
    return None


async def _acquire(client, message, users, chats):
    """
    Fetch the payload by every available route.

    Returns `(buffer | None, spec | None, reason)`. The reason accumulates what
    each route said, because that string is the only thing that will exist
    after the media is gone.
    """
    tried = []

    spec = inspect_media(client, message.media)
    if spec is None:
        tried.append("update carried no payload")
    else:
        buf = await _fetch(client, spec, "direct", tried)
        if buf is not None:
            return buf, spec, ""

    # A fresh file reference. Also the route that covers a payload-less update.
    try:
        fresh_media = await _refetch_media(client, message.id)
    except FloodWait:
        raise
    except Exception as e:
        # Keep the reasons collected so far: they are what the report is made
        # of, and losing them to a failed re-read would be losing the evidence.
        fresh_media = None
        tried.append(f"re-read ({type(e).__name__}: {e})")

    fresh = inspect_media(client, fresh_media)

    if fresh is None:
        tried.append("re-read returned no payload")
    elif spec is not None and fresh.file_id == spec.file_id:
        tried.append("re-read returned the same reference")
    else:
        buf = await _fetch(client, fresh, "re-read", tried)
        if buf is not None:
            return buf, fresh, ""
        spec = fresh

    try:
        buf, why = await _download_via_parser(client, message, users, chats)
        if buf is not None:
            log.info("TTL media needed the high-level parser route")
            return buf, spec, ""
        tried.append(f"parser ({why})")
    except Exception as e:
        tried.append(f"parser ({type(e).__name__}: {e})")

    return None, spec, " | ".join(tried)


def _sweep_local_copies() -> int:
    """
    Delete local copies older than the retention window. Returns the count.

    The sweep runs on write rather than on a timer, because writing is the only
    thing that makes the directory grow: a bot that rescues nothing needs no
    cleanup. Failures are swallowed per file — one undeletable file must not
    stop the rest, and none of this may endanger the rescue that triggered it.
    """
    cutoff = time.time() - config.TTL_MEDIA_RETENTION_DAYS * 86400
    removed = 0

    try:
        entries = list(paths.TTL_MEDIA_DIR.iterdir())
    except OSError:
        return 0

    for entry in entries:
        try:
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                entry.unlink()
                removed += 1
        except OSError as e:
            log.warning("could not remove the old local copy %s: %s", entry.name, type(e).__name__)

    if removed:
        log.info(
            "removed %s local cop%s older than %s day(s)",
            removed, "y" if removed == 1 else "ies", config.TTL_MEDIA_RETENTION_DAYS,
        )

    return removed


async def _save_local(buf, chat_id: int, message_id: int) -> str:
    """
    Keep a copy of the payload under `data/ttl_media/`.

    Secondary to the upload: failures are logged and never interrupt it.
    """
    try:
        paths.TTL_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        _sweep_local_copies()

        # The name is already sanitised in _upload_name; this is the second
        # line of defence, because the value originates with the sender.
        original = _safe_name(getattr(buf, "name", "")) or "media.bin"
        stamp = time_text(fmt="%Y%m%d-%H%M%S")
        target = paths.TTL_MEDIA_DIR / f"{stamp}_{chat_id}_{message_id}_{original}"

        buf.seek(0)
        target.write_bytes(buf.read())
        return str(target)
    except Exception as e:
        log.warning("Local copy of TTL media failed: %s", type(e).__name__)
        return ""


async def _resend_to_saved(client, spec: _Media, buf, caption: str) -> str:
    """
    Re-upload the payload to Saved Messages. Returns a note about any fallback.

    Forwarding is deliberately avoided: forwarding TTL media is either rejected
    outright or carries the timer over.

    Every sender is given the original duration and dimensions. Pyrogram
    defaults them to zero (and `length=1` for a video note), which is what
    turned a saved video note into a 1x1, 0-second circle and a saved voice
    into a 0:00 clip.

    The cascade is per kind and degrades on purpose: a round video that
    Telegram refuses as a video note is still worth having as a video, and
    anything at all is still worth having as a file. Each step down is named in
    the returned note, so a degraded rescue never looks like a clean one.
    """
    kind = spec.kind

    async def as_document(reason: str) -> str:
        note = f"⚠️ به‌صورت فایل ذخیره شد ({reason})"
        buf.seek(0)
        await client.send_document(
            "me", buf,
            caption=f"{caption}\n{html.escape(note)}",
            parse_mode=ParseMode.HTML,
            force_document=True,
        )
        return note

    buf.seek(0)

    try:
        if kind == "photo":
            # ttl_seconds is deliberately not passed — the saved copy must not
            # inherit the timer it was rescued from.
            await client.send_photo("me", buf, caption=caption, parse_mode=ParseMode.HTML)

        elif kind == "voice":
            await client.send_voice(
                "me", buf, caption=caption, parse_mode=ParseMode.HTML,
                duration=spec.duration,
            )

        elif kind == "video":
            await client.send_video(
                "me", buf, caption=caption, parse_mode=ParseMode.HTML,
                duration=spec.duration, width=spec.width, height=spec.height,
                supports_streaming=spec.supports_streaming,
            )

        elif kind == "animation":
            await client.send_animation(
                "me", buf, caption=caption, parse_mode=ParseMode.HTML,
                duration=spec.duration, width=spec.width, height=spec.height,
            )

        elif kind == "audio":
            await client.send_audio(
                "me", buf, caption=caption, parse_mode=ParseMode.HTML,
                duration=spec.duration,
                performer=spec.performer or None, title=spec.title or None,
            )

        elif kind == "video_note":
            try:
                # send_video_note is the one sender with no caption parameter,
                # so the report follows as its own message.
                await client.send_video_note(
                    "me", buf,
                    duration=spec.duration,
                    length=spec.width or DEFAULT_NOTE_LENGTH,
                )
            except FloodWait:
                raise
            except Exception as e:
                # A round video Telegram will not take as a video note is still
                # a perfectly good video; only its shape is lost.
                log.warning(
                    "send_video_note refused the rescue (%s: %s); retrying as a video",
                    type(e).__name__, e,
                )
                note = f"⚠️ به‌شکل ویدیوی معمولی ذخیره شد ({type(e).__name__})"
                buf.seek(0)
                await client.send_video(
                    "me", buf,
                    caption=f"{caption}\n{html.escape(note)}",
                    parse_mode=ParseMode.HTML,
                    duration=spec.duration,
                    width=spec.width or DEFAULT_NOTE_LENGTH,
                    height=spec.height or DEFAULT_NOTE_LENGTH,
                )
                return note

            try:
                await client.send_message("me", caption, parse_mode=ParseMode.HTML)
            except Exception as e:
                # The circle is already saved; losing its caption must not send
                # the caller down the fallback path and upload it a second time.
                log.warning("Video-note report message failed: %s", type(e).__name__)

        else:
            await client.send_document(
                "me", buf, caption=caption, parse_mode=ParseMode.HTML,
                force_document=True,
            )

        return ""

    except FloodWait:
        # Handled by the caller, which knows how long it is willing to wait.
        raise
    except Exception as e:
        # The name has to be kept here: Python unbinds `e` at the end of the
        # except block, so it does not exist below.
        failure = type(e).__name__
        log.warning(
            "Sending TTL media as %s failed (%s: %s); falling back to a plain document",
            kind, failure, e,
        )

    # Last resort: whatever the format was, the bytes still get saved.
    return await as_document(failure)


def _peer(users, user_id: int):
    """The sender's entry in the update's own user map, or None."""
    try:
        return users.get(user_id)
    except Exception:
        # `users` is handed to us by the dispatcher; a shape that does not
        # answer .get() must not cost the rescue its report.
        return None


def _sender_name(user) -> str:
    """Display name of the sender."""
    try:
        first = getattr(user, "first_name", "") or ""
        last = getattr(user, "last_name", "") or ""
        return (first + " " + last).strip() or "ناشناس"
    except Exception:
        return "ناشناس"


def _sender_block(users, user_id: int) -> str:
    """
    Who the media came from: the display name, then the lines that identify
    the account itself.

    The username used to be glued onto the display name and the numeric id was
    left out altogether. Both now go through contact_lines(), so a rescued
    one-time photo says who sent it in the same terms everywhere — and a
    sender with no username and no shared phone number is still identifiable
    by number rather than by a display name he can change.
    """
    user = _peer(users, user_id)
    return (
        f"👤 <b>فرستنده:</b> {html.escape(_sender_name(user))}\n"
        + contact_lines(user, user_id)
    )


def _header(kind: str, ttl: int, sender: str) -> str:
    """
    Common first block of both the success report and the failure report.

    `sender` arrives ready-made from _sender_block() — name and identity
    lines, already escaped.
    """
    return (
        f"{sender}\n"
        f"📎 <b>نوع:</b> {_KIND_LABELS.get(kind, kind)}\n"
        f"⏱ <b>تایمر:</b> {_describe_ttl(ttl)}\n"
        f"🕐 <b>زمان:</b> {time_text()}"
    )


def _build_caption(spec: _Media, ttl: int, sender: str,
                   original_caption: str) -> str:
    """
    Build the report that accompanies the saved media.

    HTML with html.escape() on everything that comes from the other party
    (§10.8). The length is budgeted as well: a media caption is capped at 1024
    characters, and escaping quadruples a `<`, so an over-long original caption
    would make Telegram reject the whole upload — and with it the rescue.

    The path of the local copy is deliberately *not* included: it is a
    container-internal path, it means nothing in a chat, and it pushes the
    caption towards the limit for no benefit. It goes to the log instead.
    """
    text = "🕵️ <b>مدیای زمان‌دار ذخیره شد</b>\n\n" + _header(
        spec.kind, ttl, sender
    )

    if original_caption:
        room = CAPTION_LIMIT - NOTE_ROOM - len(text) - len("\n💬 <b>کپشن:</b> …")
        if room > 0:
            escaped = html.escape(original_caption)
            if len(escaped) > room:
                # Cutting escaped text could split an entity like &amp; in
                # half, so the raw text is cut first and escaped afterwards.
                # The quarter is the worst case of html.escape.
                escaped = html.escape(original_caption[: max(0, room // 4)]) + "…"
            text += f"\n💬 <b>کپشن:</b> {escaped}"

    return text


async def _report_failure(client, kind, ttl, sender,
                          reason: str, media) -> None:
    """
    Tell the user, in Saved Messages, that a rescue did not happen.

    This is not decoration. A missed rescue is invisible by nature — nothing
    arrives, and the media is gone before anyone could look at a log — so the
    reason has to be delivered to the same place the media would have been.
    """
    text = (
        "⚠️ <b>مدیای زمان‌دار ذخیره نشد</b>\n\n"
        + _header(kind, ttl, sender)
        + f"\n❗️ <b>دلیل:</b> <code>{html.escape(reason)}</code>\n\n"
        "اگر پیام روی گوشی یا دستگاه دیگری زودتر باز شده باشد، فایل دیگر روی "
        "سرور در دسترس نیست و هیچ راهی برای بازیابی‌اش نمانده."
    )

    if config.TTL_GUARD_DEBUG:
        text += f"\n\n<pre>{html.escape(_dump(media))}</pre>"
    else:
        text += "\n\nبرای گزارش دقیق‌تر <code>TTL_GUARD_DEBUG=true</code> را در .env بگذارید."

    try:
        await client.send_message("me", text, parse_mode=ParseMode.HTML)
    except Exception as e:
        log.warning("Could not deliver the TTL failure report: %s", type(e).__name__)


@Client.on_raw_update(group=TTL_GROUP)
async def ttl_media_guard(client, update, users, chats):
    """Intercept timed media at the raw MTProto layer, before it is read."""
    if not config.TTL_GUARD:
        return

    if not isinstance(update, raw.types.UpdateNewMessage):
        return

    message = update.message
    if not isinstance(message, raw.types.Message):
        return

    # Cheapest discriminator first: this handler sees every single update.
    ttl = getattr(message.media, "ttl_seconds", None)
    if not ttl:
        # The one hypothesis this module cannot test for itself: media the
        # pinned layer (158) cannot describe. Telegram down-converts anything
        # newer for us, and if a future one-time format ever arrives as
        # messageMediaUnsupported instead of carrying ttl_seconds, nothing else
        # here would ever mention it. Debug-gated, because genuinely
        # unsupported media (new poll kinds, giveaways) also lands here.
        if (
            config.TTL_GUARD_DEBUG
            and isinstance(message.media, raw.types.MessageMediaUnsupported)
            and not message.out
            and isinstance(message.peer_id, raw.types.PeerUser)
        ):
            log.info(
                "Unsupported private media in chat %s — pinned layer %s cannot "
                "describe it; if this coincides with a one-time message, the "
                "format is newer than the library",
                message.peer_id.user_id, raw.all.layer,
            )
        return

    # Our own outgoing media is already on this machine.
    if message.out:
        return

    # Private chats only. Groups and channels have no user-to-user TTL media.
    if not isinstance(message.peer_id, raw.types.PeerUser):
        return

    chat_id = message.peer_id.user_id

    # Saved Messages is our own notepad.
    me = getattr(client, "me", None)
    if me and chat_id == me.id:
        return

    if _seen_before((chat_id, message.id)):
        return

    log.info(
        "TTL media in chat %s (ttl=%s, media=%s)",
        chat_id, ttl, type(message.media).__name__,
    )
    if config.TTL_GUARD_DEBUG:
        log.info("TTL media structure: %s", _dump(message.media))

    sender = _sender_block(users, chat_id)
    spec = None
    kind = "?"

    try:
        buf, spec, reason = await _acquire(client, message, users, chats)
        kind = spec.kind if spec else "?"

    except FloodWait as e:
        delay = flood_seconds(e)
        log.warning("FloodWait %.0fs while downloading TTL media", delay)
        if delay <= FLOOD_WAIT_LIMIT:
            await asyncio.sleep(delay)
        _PROCESSED.pop((chat_id, message.id), None)  # let a re-delivery retry
        _record_event(kind, "FloodWait", f"{delay:.0f}s")
        return
    except Exception as e:
        _STATS["failed"] += 1
        detail = f"{type(e).__name__}: {e}"
        log.warning("TTL media download failed in chat %s: %s", chat_id, detail)
        _record_event(kind, "خطا در دانلود", detail)
        await _report_failure(client, kind, ttl, sender,
                              detail, message.media)
        return

    if buf is None:
        # Every route is exhausted: the payload is not on the server any more,
        # which normally means it was opened on another device first.
        _STATS["lost"] += 1
        log.warning("TTL media in chat %s could not be fetched: %s", chat_id, reason)
        _record_event(kind, "دانلود نشد", reason)
        await _report_failure(client, kind, ttl, sender,
                              reason, message.media)
        return

    if config.TTL_GUARD_LOCAL_COPY:
        local_path = await _save_local(buf, chat_id, message.id)
        if local_path:
            # English, and only in the log: the path is a container path and
            # has no meaning inside a Telegram message.
            log.info("kept a local copy at %s", local_path)

    caption = _build_caption(spec, ttl, sender, message.message or "")

    try:
        note = await _resend_to_saved(client, spec, buf, caption)
        _STATS["saved"] += 1
        _record_event(kind, "ذخیره شد", note)
        log.info("Saved TTL media (%s) from chat %s (ttl=%s)", spec.kind, chat_id, ttl)
    except FloodWait as e:
        delay = flood_seconds(e)
        log.warning("FloodWait %.0fs while saving TTL media", delay)
        if delay > FLOOD_WAIT_LIMIT:
            _STATS["failed"] += 1
            _record_event(kind, "FloodWait در ارسال", f"{delay:.0f}s")
            return
        await asyncio.sleep(delay)
        try:
            await _resend_to_saved(client, spec, buf, caption)
            _STATS["saved"] += 1
            _record_event(kind, "ذخیره شد (بعد از FloodWait)")
        except Exception as e2:
            _STATS["failed"] += 1
            detail = f"{type(e2).__name__}: {e2}"
            log.warning("Saving TTL media failed after FloodWait: %s", detail)
            _record_event(kind, "خطا در ارسال", detail)
            await _report_failure(client, kind, ttl, sender,
                                  detail, message.media)
    except Exception as e:
        _STATS["failed"] += 1
        detail = f"{type(e).__name__}: {e}"
        log.warning("Saving TTL media failed: %s", detail)
        _record_event(kind, "خطا در ارسال", detail)
        await _report_failure(client, kind, ttl, sender,
                              detail, message.media)
    finally:
        try:
            buf.close()
        except Exception:
            pass


# --- Status command ---
# The authorization filter is imported from general.py so the rule is defined
# in exactly one place.
from plugins.general import AUTH_FILTER  # noqa: E402


@Client.on_message(AUTH_FILTER & filters.command(["ttl", "ttlguard"], prefixes=["/", "."]))
@safe_handler
async def ttl_status_command(client, message):
    """Show the guard's state, this run's counters and the last outcomes."""
    state = "🟢 روشن" if config.TTL_GUARD else "🔴 خاموش"
    local = "روشن" if config.TTL_GUARD_LOCAL_COPY else "خاموش"
    debug = "روشن" if config.TTL_GUARD_DEBUG else "خاموش"

    lines = [
        "🕵️ **نگهبان مدیای زمان‌دار**",
        "",
        f"وضعیت: {state}",
        f"کپی محلی: {local}" + (
            f" (نگهداری {config.TTL_MEDIA_RETENTION_DAYS} روز)"
            if config.TTL_GUARD_LOCAL_COPY else ""
        ),
        f"گزارش فنی: {debug}",
        f"ذخیره‌شده در این اجرا: {_STATS['saved']}",
        f"ناموفق: {_STATS['failed']}",
        f"از دست رفته (قبل از دانلود باز شده): {_STATS['lost']}",
    ]

    if _LAST_EVENTS:
        lines += ["", "**آخرین رویدادها:**"]
        for stamp, kind, outcome, detail in _LAST_EVENTS:
            row = f"`{stamp}` · {kind} · {outcome}"
            if detail:
                row += f" — `{detail}`"
            lines.append(row)

    lines += [
        "",
        "پوشش: عکس، ویدیو، ویدیو مسیج، ویس، گیف، فایل صوتی و فایل",
        "— هم تایمردار و هم «فقط یک بار»",
        "",
        "تنظیم از طریق `TTL_GUARD` در فایل `.env`",
    ]

    await respond(message, "\n".join(lines))
