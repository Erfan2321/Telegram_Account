"""
Forward a message by its link — public or private, channel or group.

    .fwd https://t.me/c/1234567890/42
    .fwd https://t.me/somechannel/42 https://t.me/c/1234567890/7/99

The message lands in the chat the command is typed in (inside the same topic,
in a forum), and the command itself is deleted once everything arrived, so
only the forwarded message is left. `.fwd` with no link, sent as a reply,
takes the link out of the replied message instead.

Accepted forms: t.me / telegram.me / telegram.dog, public (`/<username>/<id>`)
and private (`/c/<id>/<id>`), with or without a topic segment; `?single` (only
that item of an album — otherwise the whole album comes along);
`?comment=<id>` (a comment under a channel post); and the `tg://privatepost`
and `tg://resolve` forms.

A private link carries nothing but the numeric chat id:
    The session can only reach a chat whose access hash it has stored, and a
    channel you belong to that has produced no update since the session was
    created is not stored yet. So a failed lookup pages through your own
    dialog list once — `client.invoke` stores every chat on each page — and
    tries again.

Protected content ("Restrict saving content"):
    Telegram refuses to forward such messages, but it still serves the files
    to every member who can see them. So a protected message is downloaded and
    uploaded again with its caption, formatting, duration, dimensions and
    preview — a copy, without the "forwarded from" header. Text, polls,
    locations and contacts need no download and are simply re-sent.

Owner only (`filters.me`): an authorized third party could otherwise pull
content out of every private channel this account belongs to. The command
message also has to be ours, to be deleted at the end.
"""

import functools
import html
import logging
import os
import re
import shutil
import tempfile
import time
from typing import NamedTuple
from urllib.parse import parse_qs

from pyrogram import Client, filters, raw, types, utils
from pyrogram.enums import ParseMode
from pyrogram.errors import (
    ChannelInvalid,
    ChannelPrivate,
    ChatForwardsRestricted,
    FloodWait,
    MessageIdInvalid,
    PeerIdInvalid,
    RPCError,
    UsernameInvalid,
    UsernameNotOccupied,
)

from utils import paths
from utils.helpers import describe, flood_seconds, respond, safe_delete, safe_handler

log = logging.getLogger(__name__)

PREFIXES = ["/", "."]
COMMANDS = ["fwd", "forward"]

# Links handled by one command. Bounds the work a single paste can start.
MAX_LINKS = 10

# Dialog paging for a private link the session has not seen yet — the same
# numbers as group_admin's scan: 100 per page is Telegram's maximum, and 60
# pages (6000 chats) is far beyond any real account.
DIALOG_PAGE = 100
MAX_DIALOG_PAGES = 60

# Minimum seconds between two progress edits of the command message. Each edit
# is a request; a 1 GB upload must not turn into a FloodWait of its own.
PROGRESS_INTERVAL = 4.0

# Telegram's upload ceilings (MiB). A file above the account's ceiling cannot
# be sent again, so it is refused before a single byte is downloaded.
UPLOAD_LIMIT_MB = 2000
PREMIUM_UPLOAD_LIMIT_MB = 4000

# What Telegram accepts as a custom preview: a JPEG of at most 200 KB and 320 px
# per side. Telegram's own "m" previews fit; anything larger is skipped.
THUMB_MAX_BYTES = 200 * 1024
THUMB_MAX_SIDE = 320

# Pyrogram's default video-note length is 1, which renders as a dot.
DEFAULT_NOTE_LENGTH = 384

# Scratch entries older than this were left by a run that died mid-way.
STALE_SCRATCH_SECONDS = 24 * 3600

# How long an expected echo of our own copy stays registered (see _ECHOES).
ECHO_TTL = 600

# Characters of a link quoted back in a failure report.
LINK_SHOWN = 80

# Media that has to travel as a file. Exactly one of these attributes is set
# on a media message, so their order does not matter beyond readability.
MEDIA_KINDS = ("photo", "video", "animation", "audio", "voice", "video_note", "sticker", "document")

# What send_media_group accepts. A protected album holding anything else is
# sent item by item instead.
ALBUM_KINDS = ("photo", "video", "audio", "document")

# Kinds that show a preview image.
THUMB_KINDS = ("video", "animation", "audio", "document", "video_note")

# Extension for a downloaded file whose mime type maps to nothing. It matters:
# the senders re-guess the mime type from it, and a voice saved as .mp3 is
# uploaded as audio/mpeg.
_EXT_FALLBACK = {
    "photo": ".jpg",
    "video": ".mp4",
    "animation": ".mp4",
    "audio": ".mp3",
    "voice": ".ogg",
    "video_note": ".mp4",
    "sticker": ".webp",
    "document": ".bin",
}

# --- What the user reads ---
_USAGE = (
    "❌ لینک پیام را بعد از دستور بنویس، یا روی پیامی که لینک دارد ریپلای کن.\n"
    "مثال: `.fwd https://t.me/c/1234567890/42`\n"
    f"تا {MAX_LINKS} لینک را می‌شود با فاصله پشت هم نوشت."
)
_NOT_CHANNEL = "این لینک به پیامی در کانال یا گروه اشاره نمی‌کند."
_NO_DISCUSSION = "این کانال گروه بحث ندارد، پس لینک کامنت به جایی نمی‌رسد."
_NO_MESSAGE = "پیام پیدا نشد؛ شاید پاک شده باشد."
_SERVICE = "این یک پیام سرویسی است و قابل انتقال نیست."
_UNSUPPORTED = "این نوع پیام از کانال محافظت‌شده قابل کپی نیست."
_NOT_MEMBER = "عضو این کانال یا گروه نیستی (یا از آن بیرون شده‌ای)."
_NO_USERNAME = "کانال یا گروهی با این یوزرنیم وجود ندارد."
_UNREACHABLE = "این چت در دسترس نیست؛ یا عضوش نیستی یا لینک اشتباه است."
_DOWNLOAD_FAILED = "دانلود فایل از تلگرام ناموفق بود."
_REJECTED = "تلگرام درخواست را رد کرد."
_UNEXPECTED = "خطای غیرمنتظره."
_PROTECTED_HEADER = (
    "🔒 محتوای محافظت‌شده است؛ فایل دانلود و دوباره فرستاده می‌شود (بدون عنوان فوروارد)."
)


class LinkError(Exception):
    """
    A link that cannot be served.

    str() is English, for the log; `.text` is the Persian line the user reads,
    and `.detail` an optional technical tail shown under it. Keeping the
    languages in separate fields is what keeps this exception inside the
    project's language rule instead of making it a second MediaError.
    """

    def __init__(self, reason: str, text: str, detail: str = ""):
        super().__init__(reason)
        self.text = text
        self.detail = detail


class _Link(NamedTuple):
    chat: int | str     # -100… id for a private link, a username otherwise
    message_id: int
    single: bool        # ?single: only this item, not its whole album
    comment: int | None # ?comment=N: a comment in the post's discussion group
    text: str           # the link as written, quoted back in a failure report


# --- Link parsing ---

_HTTP_LINK = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/"
    r"(?:c/(?P<cid>\d+)|(?:s/)?(?P<user>[a-z][a-z0-9_]{3,31}))"
    # One segment is the message, two are topic + message. Anything deeper is
    # not a message link, and the lookahead refuses it instead of quietly
    # taking a wrong number out of the middle.
    r"(?P<path>(?:/\d+){1,2})(?!\d|/\d)"
    r"(?P<query>\?[^\s#]*)?",
    re.IGNORECASE,
)

_TG_LINK = re.compile(r"tg://(?P<kind>privatepost|resolve)\?(?P<query>[^\s#]+)", re.IGNORECASE)

_USERNAME = re.compile(r"[a-z][a-z0-9_]{3,31}", re.IGNORECASE)

# Punctuation that ends a sentence rather than a query: "…/42?single." has to
# keep its `single`.
_TRAILING = ".,;:!?)]}»«'\"،؛؟"


def _query(raw_query) -> dict:
    return parse_qs((raw_query or "").lstrip("?").rstrip(_TRAILING), keep_blank_values=True)


def _int_param(params: dict, name: str) -> int | None:
    """A positive integer query parameter, or None."""
    try:
        value = int((params.get(name) or [""])[0])
    except ValueError:
        return None
    return value if value > 0 else None


def _links_in(text: str) -> list:
    """Every message link in `text`, in order of appearance, without repeats."""
    found = []

    for match in _HTTP_LINK.finditer(text or ""):
        params = _query(match.group("query"))
        message_id = int(match.group("path").rsplit("/", 1)[1])
        if message_id <= 0:
            continue
        cid = match.group("cid")
        chat = utils.get_channel_id(int(cid)) if cid else match.group("user")
        found.append((match.start(), _Link(
            chat, message_id, "single" in params, _int_param(params, "comment"), match.group(0),
        )))

    for match in _TG_LINK.finditer(text or ""):
        params = _query(match.group("query"))
        message_id = _int_param(params, "post")
        if message_id is None:
            continue
        if match.group("kind").lower() == "privatepost":
            channel = _int_param(params, "channel")
            if channel is None:
                continue
            chat = utils.get_channel_id(channel)
        else:
            chat = (params.get("domain") or [""])[0]
            if not _USERNAME.fullmatch(chat):
                continue
        found.append((match.start(), _Link(
            chat, message_id, "single" in params, _int_param(params, "comment"), match.group(0),
        )))

    found.sort(key=lambda pair: pair[0])

    links, seen = [], set()
    for _, link in found:
        key = (
            link.chat.lower() if isinstance(link.chat, str) else link.chat,
            link.message_id, link.single, link.comment,
        )
        if key not in seen:
            seen.add(key)
            links.append(link)
    return links


def _links_of(message) -> list:
    """Links in a message's text or caption, including hidden hyperlinks."""
    if message is None:
        return []

    parts = [str(message.text or message.caption or "")]
    for entity in message.entities or message.caption_entities or []:
        url = getattr(entity, "url", None)
        if url:
            parts.append(str(url))

    return _links_in("\n".join(parts))


# --- Echo guard ---
# A copy is a brand-new message of ours. If its text or caption happens to be
# ".fwd <link>", it matches this very command — and a post linking to itself
# would then forward itself forever. Forwarded messages are kept out by the
# filter (~filters.forwarded); copies register their text here *before* they
# are sent, since the update can arrive before the send call returns, and the
# handler consumes the entry. The same one-shot pattern as IGNORE_DELETED_IDS.
_ECHOES = {}  # {(chat_id, text): monotonic expiry}

_COMMAND_LIKE = re.compile(r"^[/.](?:fwd|forward)(?:@\w+)?(?:\s|$)", re.IGNORECASE)


def _expect_echo(chat_id: int, text) -> None:
    if not text or not _COMMAND_LIKE.match(str(text)):
        return

    now = time.monotonic()
    for key, expiry in list(_ECHOES.items()):
        if expiry <= now:
            _ECHOES.pop(key, None)
    _ECHOES[(chat_id, str(text))] = now + ECHO_TTL


def _is_echo(chat_id: int, text) -> bool:
    if not text:
        return False
    expiry = _ECHOES.pop((chat_id, str(text)), None)
    return expiry is not None and expiry > time.monotonic()


# --- Resolving the chat ---

# What a lookup raises when the session does not (yet) know the chat. For a
# private link, ChannelPrivate belongs here too: a stale stored hash answers
# with it, and the dialog list carries a fresh one.
_UNRESOLVED = (PeerIdInvalid, ChannelInvalid, ChannelPrivate, KeyError, ValueError)


def _explain(error: BaseException) -> LinkError:
    """Turn a failed lookup into what the user is told."""
    reason = describe(error)

    if isinstance(error, ChannelPrivate):
        return LinkError(reason, _NOT_MEMBER)
    if isinstance(error, (UsernameNotOccupied, UsernameInvalid)):
        return LinkError(reason, _NO_USERNAME)
    if isinstance(error, MessageIdInvalid):
        return LinkError(reason, _NO_MESSAGE)
    if isinstance(error, (PeerIdInvalid, ChannelInvalid, KeyError, ValueError)):
        return LinkError(reason, _UNREACHABLE)
    return LinkError(reason, _REJECTED, detail=reason)


async def _load_from_dialogs(client, chat_id: int) -> bool:
    """
    Page through the dialog list until `chat_id` shows up. True if it did.

    Finding it is the whole point: `client.invoke` hands every page's chats
    to fetch_peers, so once the page holding the chat has been read, the chat
    resolves. Raw GetDialogs rather than `get_dialogs()`, because the
    high-level generator parses every dialog's top message — fetching its
    reply too — which is a request per dialog for nothing. Cursor and guards
    follow group_admin._scan_managed_groups.
    """
    # get_channel_id is its own inverse: -100<n> maps back to n.
    wanted = utils.get_channel_id(chat_id)

    offset_date = 0
    offset_id = 0
    offset_peer = raw.types.InputPeerEmpty()
    seen = set()

    for _page in range(MAX_DIALOG_PAGES):
        r = await client.invoke(
            raw.functions.messages.GetDialogs(
                offset_date=offset_date,
                offset_id=offset_id,
                offset_peer=offset_peer,
                limit=DIALOG_PAGE,
                hash=0,
            ),
            sleep_threshold=60,
        )

        if any(
            isinstance(chat, raw.types.Channel) and chat.id == wanted
            for chat in getattr(r, "chats", [])
        ):
            return True

        # A Dialogs (rather than DialogsSlice) response is the entire list.
        if not isinstance(r, raw.types.messages.DialogsSlice):
            return False

        dialogs = [d for d in getattr(r, "dialogs", []) if isinstance(d, raw.types.Dialog)]
        if not dialogs:
            return False

        last_peer_id = utils.get_peer_id(dialogs[-1].peer)
        if last_peer_id in seen:
            # The cursor did not advance; stop instead of spinning.
            return False
        seen.add(last_peer_id)

        messages = {
            utils.get_peer_id(m.peer_id): m
            for m in getattr(r, "messages", [])
            if not isinstance(m, raw.types.MessageEmpty)
        }
        last_message = messages.get(last_peer_id)
        if last_message is None:
            return False

        offset_id = last_message.id
        offset_date = last_message.date
        offset_peer = await client.resolve_peer(last_peer_id)

    return False


async def _with_chat(client, chat, call):
    """
    Run `call()`; if the session does not know `chat` yet, learn it once from
    the dialog list and run it again. Every failure leaves as a LinkError,
    except FloodWait, which the command handles for the whole batch.
    """
    try:
        return await call()
    except FloodWait:
        raise
    except _UNRESOLVED as first:
        if not isinstance(chat, int):
            raise _explain(first) from None
        log.info("fwd: chat %s is not in the session (%s); looking through the dialogs",
                 chat, type(first).__name__)
        if not await _load_from_dialogs(client, chat):
            raise _explain(first) from None
    except RPCError as e:
        raise _explain(e) from None

    try:
        return await call()
    except FloodWait:
        raise
    except (RPCError, KeyError, ValueError) as e:
        raise _explain(e) from None


async def _fetch(client, link: _Link):
    """
    The source chat id and the message(s) a link points at — the whole album
    unless the link says `?single`.
    """
    chat, message_id = link.chat, link.message_id

    if link.comment:
        channel = await _with_chat(client, chat, functools.partial(client.get_chat, chat))
        linked = getattr(channel, "linked_chat", None)
        if linked is None:
            raise LinkError(f"{chat!r} has no discussion group", _NO_DISCUSSION)
        chat, message_id = linked.id, link.comment

    peer = await _with_chat(client, chat, functools.partial(client.resolve_peer, chat))
    if not isinstance(peer, raw.types.InputPeerChannel):
        # t.me/<name>/<n> also "works" for a user or a bot, and the request it
        # turns into (messages.getMessages) ignores the chat entirely: it would
        # return message <n> of some private chat of this account.
        raise LinkError(f"{chat!r} is not a channel or a group", _NOT_CHANNEL)

    chat_id = utils.get_channel_id(peer.channel_id)
    message = await _with_chat(
        client, chat_id,
        functools.partial(client.get_messages, chat_id, message_id, replies=0),
    )

    if message is None or getattr(message, "empty", False):
        raise LinkError(f"message {message_id} not found in {chat_id}", _NO_MESSAGE)
    if getattr(message.chat, "id", None) != chat_id:
        raise LinkError(f"message {message_id} came back from another chat", _NO_MESSAGE)
    if message.service:
        raise LinkError(f"message {message_id} is a service message", _SERVICE)

    if message.media_group_id and not link.single:
        try:
            album = await client.get_media_group(chat_id, message.id)
        except FloodWait:
            raise
        except (RPCError, ValueError) as e:
            log.info("fwd: album of %s/%s not readable (%s); sending the one message",
                     chat_id, message.id, describe(e))
        else:
            album = sorted(
                (m for m in album if not getattr(m, "empty", False)),
                key=lambda m: m.id,
            )
            if album:
                return chat_id, album

    return chat_id, [message]


# --- Progress and scratch space ---

def _mb(size: int) -> str:
    return f"{size / 1048576:.1f}"


class _Progress:
    """
    Transfer status, edited into the command message at most every
    PROGRESS_INTERVAL seconds.

    The edit goes to `edit_text` directly instead of through respond(): this
    runs inside the download/upload loop, and respond() would sleep out a
    FloodWait right there, stalling the transfer it is reporting on. A status
    line is cosmetic, so a failed edit is dropped and the next tick tries
    again.
    """

    def __init__(self, message):
        self.message = message
        self.header = ""
        self._last = 0.0

    async def say(self, text: str, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last < PROGRESS_INTERVAL:
            return
        self._last = now

        body = f"{self.header}\n{text}" if self.header else text
        try:
            await self.message.edit_text(body, parse_mode=ParseMode.DISABLED)
        except Exception as e:
            log.debug("fwd: progress edit skipped: %s", describe(e))

    async def transfer(self, current: int, total: int, verb: str, label: str) -> None:
        """Pyrogram's progress callback: (current, total, *progress_args)."""
        where = f"{verb} {label}".strip()
        if total:
            text = f"{where}: {current * 100 // total}% ({_mb(current)} از {_mb(total)} مگابایت)"
        else:
            text = f"{where}: {_mb(current)} مگابایت"
        await self.say(text)


def _sweep_stale_scratch() -> None:
    """
    Remove what a run that died mid-way left behind.

    A run removes its own directory in a `finally`, so only a hard kill leaves
    anything here; a day is far longer than any run lasts, so nothing in use
    is ever touched. Failures are swallowed — tidying must not cost the run
    that triggered it.
    """
    cutoff = time.time() - STALE_SCRATCH_SECONDS
    try:
        entries = list(paths.FORWARD_TMP_DIR.iterdir())
    except OSError:
        return

    for entry in entries:
        try:
            if entry.stat().st_mtime >= cutoff:
                continue
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink()
        except OSError:
            continue


class _Scratch:
    """
    The run's scratch directory: made on first use, removed when the run ends.

    Its own subdirectory per run, so two `.fwd` commands at once never touch
    each other's files.
    """

    def __init__(self):
        self.path = None

    def get(self) -> str:
        if self.path is None:
            paths.FORWARD_TMP_DIR.mkdir(parents=True, exist_ok=True)
            _sweep_stale_scratch()
            self.path = tempfile.mkdtemp(prefix="run_", dir=paths.FORWARD_TMP_DIR)
        return self.path

    def cleanup(self) -> None:
        if self.path is not None:
            shutil.rmtree(self.path, ignore_errors=True)
            self.path = None


# --- Protected content: download and send again ---

def _kind(message) -> str | None:
    for kind in MEDIA_KINDS:
        if getattr(message, kind, None) is not None:
            return kind
    return None


def _upload_limit(client) -> int:
    me = getattr(client, "me", None)
    megabytes = PREMIUM_UPLOAD_LIMIT_MB if getattr(me, "is_premium", False) else UPLOAD_LIMIT_MB
    return megabytes * 1024 * 1024


def _local_name(client, kind: str, media, message_id: int) -> str:
    """
    A file name of our own, never the sender's.

    The original name is arbitrary text from the channel, and Pyrogram joins
    it onto the directory unchecked — `../../x` would write outside the
    scratch directory. Only the extension matters on disk (the senders re-guess
    the mime type from it), so it is derived from the declared mime type. The
    original name still travels with the upload as `file_name`, where it is
    only a label.
    """
    mime = getattr(media, "mime_type", "") or ""
    extension = (client.guess_extension(mime) if mime else None) or _EXT_FALLBACK[kind]
    return f"{message_id}_{kind}{extension}"


async def _download(client, message, kind: str, media, scratch: _Scratch,
                    progress: _Progress, label: str) -> str:
    size = getattr(media, "file_size", 0) or 0
    limit = _upload_limit(client)
    if size > limit:
        raise LinkError(
            f"{size} bytes is over the {limit}-byte upload limit",
            f"حجم فایل ({_mb(size)} مگابایت) از سقف آپلود تلگرام ({_mb(limit)} مگابایت) بیشتر است.",
        )

    target = os.path.join(scratch.get(), _local_name(client, kind, media, message.id))
    path = await client.download_media(
        message,
        file_name=target,
        progress=progress.transfer,
        progress_args=("⬇️ دانلود", label),
    )

    if not path or not os.path.isfile(path):
        raise LinkError("download produced no file", _DOWNLOAD_FAILED)
    return path


async def _download_thumb(client, kind: str, media, scratch: _Scratch, message_id: int):
    """
    The original's own preview, for the kinds that show one.

    Optional by nature: any failure means the copy goes out without a
    preview, never that it does not go out.
    """
    if kind not in THUMB_KINDS:
        return None

    candidates = [
        thumb for thumb in getattr(media, "thumbs", None) or []
        if getattr(thumb, "file_id", None)
        and max(thumb.width or 0, thumb.height or 0) <= THUMB_MAX_SIDE
        and (thumb.file_size or 0) <= THUMB_MAX_BYTES
    ]
    if not candidates:
        return None

    best = max(candidates, key=lambda thumb: (thumb.width or 0) * (thumb.height or 0))
    try:
        path = await client.download_media(
            best, file_name=os.path.join(scratch.get(), f"{message_id}_thumb.jpg"),
        )
    except FloodWait:
        return None
    except Exception as e:
        log.info("fwd: preview of message %s not downloaded (%s); sending without it",
                 message_id, describe(e))
        return None

    return path if path and os.path.isfile(path) else None


async def _upload(client, message, kind: str, media, path: str, thumb,
                  target: int, thread, progress: _Progress, label: str) -> None:
    """
    Send the downloaded file the way the original was sent.

    Every sender gets the original's duration and dimensions: Pyrogram
    defaults them to zero for a fresh upload, which is how a re-sent video
    note becomes a dot and a voice a 0:00 clip (ARCHITECTURE.md §8.2). The
    caption goes with its own entities and no parser, so formatting survives
    and a stray `*` in it is just a character.
    """
    caption = dict(
        caption=message.caption or "",
        caption_entities=message.caption_entities,
        parse_mode=ParseMode.DISABLED,
    )
    common = dict(
        message_thread_id=thread,
        progress=progress.transfer,
        progress_args=("⬆️ آپلود", label),
    )
    file_name = getattr(media, "file_name", None) or None
    spoiler = bool(message.has_media_spoiler) or None
    above = bool(message.show_caption_above_media) or None

    if kind == "photo":
        await client.send_photo(
            target, path, **caption,
            has_spoiler=spoiler, show_caption_above_media=above, **common,
        )
    elif kind == "video":
        await client.send_video(
            target, path, **caption,
            duration=media.duration or 0, width=media.width or 0, height=media.height or 0,
            thumb=thumb, file_name=file_name,
            supports_streaming=bool(media.supports_streaming),
            has_spoiler=spoiler, show_caption_above_media=above, **common,
        )
    elif kind == "animation":
        await client.send_animation(
            target, path, **caption,
            duration=media.duration or 0, width=media.width or 0, height=media.height or 0,
            thumb=thumb, file_name=file_name,
            has_spoiler=spoiler, show_caption_above_media=above, **common,
        )
    elif kind == "audio":
        await client.send_audio(
            target, path, **caption,
            duration=media.duration or 0, performer=media.performer, title=media.title,
            thumb=thumb, file_name=file_name, **common,
        )
    elif kind == "voice":
        await client.send_voice(
            target, path, **caption,
            duration=media.duration or 0, waveform=getattr(media, "waveform", None), **common,
        )
    elif kind == "video_note":
        # The one sender with no caption; a video note never has one.
        await client.send_video_note(
            target, path,
            duration=media.duration or 0, length=media.length or DEFAULT_NOTE_LENGTH,
            thumb=thumb, **common,
        )
    elif kind == "sticker":
        await client.send_sticker(target, path, emoji=media.emoji or "", **common)
    else:
        await client.send_document(
            target, path, thumb=thumb, **caption,
            file_name=file_name, force_document=True, **common,
        )


async def _replicate_one(client, message, kind, target: int, thread,
                         progress: _Progress, scratch: _Scratch, label: str) -> None:
    if kind is None:
        if not (message.text or message.poll or message.location or message.venue or message.contact):
            raise LinkError(f"nothing that can be re-sent (media: {message.media})", _UNSUPPORTED)
        _expect_echo(target, message.text)
        # Nothing to download: a copy re-creates the content from scratch,
        # which a protected chat cannot object to. The source's buttons are
        # dropped — only bots may send those.
        await message.copy(target, message_thread_id=thread, reply_markup=None)
        return

    if kind == "sticker":
        # A sticker belongs to a public set, so re-sending it by id may well
        # be allowed even out of a protected chat — and saves the transfer.
        try:
            await message.copy(target, message_thread_id=thread, reply_markup=None)
            return
        except FloodWait:
            raise
        except RPCError as e:
            log.info("fwd: sticker could not be re-sent by id (%s); uploading it", describe(e))

    media = getattr(message, kind)
    path = await _download(client, message, kind, media, scratch, progress, label)
    thumb = await _download_thumb(client, kind, media, scratch, message.id)
    _expect_echo(target, message.caption)
    await _upload(client, message, kind, media, path, thumb, target, thread, progress, label)


async def _replicate_album(client, messages, kinds, target: int, thread,
                           progress: _Progress, scratch: _Scratch) -> None:
    """Download every item, then send them as one album again."""
    items = []
    total = len(messages)

    for index, (message, kind) in enumerate(zip(messages, kinds), start=1):
        media = getattr(message, kind)
        path = await _download(client, message, kind, media, scratch, progress, f"{index}/{total}")
        thumb = await _download_thumb(client, kind, media, scratch, message.id)
        caption = dict(
            caption=message.caption or "",
            caption_entities=message.caption_entities,
            parse_mode=ParseMode.DISABLED,
        )
        file_name = getattr(media, "file_name", None) or None
        spoiler = bool(message.has_media_spoiler) or None

        if kind == "photo":
            items.append(types.InputMediaPhoto(path, **caption, has_spoiler=spoiler))
        elif kind == "video":
            items.append(types.InputMediaVideo(
                path, thumb=thumb, **caption,
                width=media.width or 0, height=media.height or 0, duration=media.duration or 0,
                file_name=file_name, supports_streaming=bool(media.supports_streaming),
                has_spoiler=spoiler,
            ))
        elif kind == "audio":
            items.append(types.InputMediaAudio(
                path, thumb=thumb, **caption,
                duration=media.duration or 0, performer=media.performer or "",
                title=media.title or "", file_name=file_name,
            ))
        else:
            items.append(types.InputMediaDocument(path, thumb=thumb, **caption, file_name=file_name))

        _expect_echo(target, message.caption)

    await progress.say(f"⬆️ آپلود آلبوم ({total} فایل)…", force=True)
    await client.send_media_group(target, items, message_thread_id=thread)


async def _replicate(client, messages, target: int, thread,
                     progress: _Progress, scratch: _Scratch) -> None:
    progress.header = _PROTECTED_HEADER
    await progress.say("⏳ آماده‌سازی…", force=True)

    kinds = [_kind(message) for message in messages]
    if len(messages) > 1 and all(kind in ALBUM_KINDS for kind in kinds):
        await _replicate_album(client, messages, kinds, target, thread, progress, scratch)
        return

    for index, (message, kind) in enumerate(zip(messages, kinds), start=1):
        label = f"{index}/{len(messages)}" if len(messages) > 1 else ""
        await _replicate_one(client, message, kind, target, thread, progress, scratch, label)


async def _deliver(client, link: _Link, target: int, thread,
                   progress: _Progress, scratch: _Scratch) -> None:
    source, messages = await _fetch(client, link)

    if not any(message.has_protected_content for message in messages):
        try:
            await client.forward_messages(
                target, source, [message.id for message in messages],
                message_thread_id=thread,
            )
            log.info("fwd: forwarded %d message(s) from %s", len(messages), source)
            return
        except ChatForwardsRestricted:
            # The per-message flag is the server's own, but the chat setting
            # is what it enforces; the refusal wins over the flag.
            log.info("fwd: %s refuses forwarding; copying instead", source)

    await _replicate(client, messages, target, thread, progress, scratch)
    log.info("fwd: copied %d protected message(s) from %s", len(messages), source)


def _report(total: int, failures) -> str:
    """What went wrong, per link. HTML, because links and details are arbitrary text."""
    parts = []

    done = total - len(failures)
    if done:
        parts.append(f"✅ {done} از {total} لینک منتقل شد.")

    for link, error in failures:
        shown = link.text if len(link.text) <= LINK_SHOWN else link.text[:LINK_SHOWN] + "…"
        part = f"❌ {html.escape(shown)}\n{html.escape(error.text)}"
        if error.detail:
            part += f"\n<code>{html.escape(error.detail)}</code>"
        parts.append(part)

    return "\n\n".join(parts)


@Client.on_message(
    filters.me & ~filters.forwarded & filters.command(COMMANDS, prefixes=PREFIXES)
)
@safe_handler
async def forward_by_link_command(client, message):
    """Bring the message(s) behind one or more links into this chat."""
    if _is_echo(message.chat.id, message.text or message.caption):
        log.info("fwd: ignoring a copy of ours that reads like a command")
        return

    links = _links_of(message) or _links_of(message.reply_to_message)
    if not links:
        await respond(message, _USAGE)
        return
    if len(links) > MAX_LINKS:
        await respond(message, f"❌ حداکثر {MAX_LINKS} لینک در هر بار.")
        return

    target = message.chat.id
    # Inside a forum topic the copies belong in that topic, not in General.
    thread = message.message_thread_id if getattr(message, "topic_message", None) else None

    progress = _Progress(message)
    scratch = _Scratch()
    failures = []

    try:
        for index, link in enumerate(links):
            progress.header = ""
            try:
                await _deliver(client, link, target, thread, progress, scratch)
            except LinkError as e:
                log.info("fwd: %s not delivered: %s", link.text, e)
                failures.append((link, e))
            except FloodWait as e:
                # Whatever is left would only earn the same wait.
                wait = flood_seconds(e)
                log.warning("fwd: FloodWait %.0fs; %d link(s) left undone", wait, len(links) - index)
                error = LinkError(
                    f"FloodWait {wait:.0f}s",
                    f"تلگرام موقتاً محدودیت گذاشته؛ حدود {wait:.0f} ثانیه دیگر دوباره امتحان کن.",
                )
                failures.extend((pending, error) for pending in links[index:])
                break
            except RPCError as e:
                log.warning("fwd: %s failed: %s", link.text, describe(e))
                failures.append((link, LinkError(describe(e), _REJECTED, detail=describe(e))))
            except Exception as e:
                log.exception("fwd: %s failed unexpectedly", link.text)
                failures.append((link, LinkError(describe(e), _UNEXPECTED, detail=describe(e))))
    finally:
        scratch.cleanup()

    if not failures:
        # Only the forwarded messages stay behind.
        await safe_delete(client, message)
        return

    await respond(message, _report(len(links), failures), parse_mode=ParseMode.HTML)
