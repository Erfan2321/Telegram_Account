"""
Handling Telegram media on its way to the AI model.

It does three things:
1. Identify a message's media (kind, size, mime) without downloading it.
2. Download safely under a size cap, and map Telegram's mime to one Google
   accepts.
3. Perform the audio conversions needed to send TTS output back to Telegram.

Unlike the other utils files this one does depend on Pyrogram, but only on its
data types, its download method and its error classes; no handler is
registered here.
"""

import asyncio
import io
import logging
import mimetypes
import shutil
import struct

from pyrogram.errors import FloodWait

log = logging.getLogger(__name__)


class MediaError(Exception):
    """
    A media error whose message is ready to show to the user.

    This is the project's one exception whose text is deliberately **Persian**,
    because its only consumer is ai_context.collect, which prints the text
    verbatim and untranslated underneath the answer. Every other error in
    utils is English.
    """


# The check order matters: stickers and animations also carry a document, so
# document has to come last or the message's real kind would be lost.
_MEDIA_ATTRS = (
    "photo", "voice", "audio", "video_note", "video",
    "animation", "sticker", "document",
)

_KIND_LABELS = {
    "photo": "عکس",
    "voice": "ویس",
    "audio": "فایل صوتی",
    "video": "ویدیو",
    "video_note": "ویدیو مسیج",
    "animation": "گیف",
    "sticker": "استیکر",
    "document": "فایل",
}

# Mime types Google accepts under one name while Telegram sends another.
_MIME_ALIASES = {
    "image/jpg": "image/jpeg",
    "image/pjpeg": "image/jpeg",
    "audio/opus": "audio/ogg",
    "audio/x-wav": "audio/wav",
    "audio/vnd.wave": "audio/wav",
    "audio/x-m4a": "audio/mp4",
    "audio/mpeg3": "audio/mpeg",
    "audio/x-mpeg": "audio/mpeg",
    "video/quicktime": "video/mov",
    "video/x-matroska": "video/webm",
    "application/json": "text/plain",
    "application/xml": "text/plain",
    "application/javascript": "text/plain",
}

# Extensions Telegram usually sends with an undetermined mime.
_EXT_FALLBACK = {
    ".py": "text/plain", ".txt": "text/plain", ".md": "text/plain",
    ".json": "text/plain", ".csv": "text/plain", ".log": "text/plain",
    ".yml": "text/plain", ".yaml": "text/plain", ".sql": "text/plain",
    ".ogg": "audio/ogg", ".oga": "audio/ogg", ".m4a": "audio/mp4",
    ".pdf": "application/pdf", ".webp": "image/webp",
}

_ALLOWED_IMAGE = {"image/png", "image/jpeg", "image/webp", "image/heic", "image/heif"}
_ALLOWED_DOC = {"application/pdf", "text/plain"}


class MediaSpec:
    """A message's media details, obtained without downloading it."""

    __slots__ = ("kind", "mime_type", "size", "file_name", "duration")

    def __init__(self, kind, mime_type, size=0, file_name=None, duration=None):
        self.kind = kind
        self.mime_type = (mime_type or "").split(";")[0].strip().lower()
        self.size = size or 0
        self.file_name = file_name
        self.duration = duration

    @property
    def label(self) -> str:
        """A short Persian description for display to the user."""
        base = _KIND_LABELS.get(self.kind, "فایل")
        if self.duration:
            return f"{base} ({self.duration} ثانیه)"
        if self.file_name:
            return f"{base} «{self.file_name}»"
        return base

    @property
    def is_audio(self) -> bool:
        return self.kind in ("voice", "audio") or self.mime_type.startswith("audio/")

    @property
    def is_image(self) -> bool:
        return self.kind in ("photo", "sticker") or self.mime_type.startswith("image/")

    @property
    def is_video(self) -> bool:
        return self.kind in ("video", "video_note", "animation") or self.mime_type.startswith("video/")

    def __repr__(self):
        return f"<MediaSpec {self.kind} {self.mime_type} {self.size}B>"


def inspect_media(message) -> MediaSpec | None:
    """Identify a message's media. Returns None when the message has none."""
    if message is None:
        return None

    for attr in _MEDIA_ATTRS:
        obj = getattr(message, attr, None)
        if obj is None:
            continue

        # Photos carry no mime; Telegram always delivers JPEG.
        mime = "image/jpeg" if attr == "photo" else getattr(obj, "mime_type", None)
        return MediaSpec(
            kind=attr,
            mime_type=mime,
            size=getattr(obj, "file_size", 0),
            file_name=getattr(obj, "file_name", None),
            duration=getattr(obj, "duration", None),
        )

    return None


def gemini_mime(spec: MediaSpec) -> str:
    """
    Map Telegram's mime to one Gemini accepts.

    An unsupported file type raises MediaError with a clear message, which
    beats sending the file and getting an opaque 400 back from Google.
    """
    mime = spec.mime_type

    # Animated (Lottie) stickers are not a standard image format at all.
    if mime in ("application/x-tgsticker", "application/x-tgs"):
        raise MediaError("❌ استیکر متحرک قابل ارسال به مدل نیست.")

    if not mime or mime == "application/octet-stream":
        guessed = None
        if spec.file_name:
            ext = "." + spec.file_name.rsplit(".", 1)[-1].lower() if "." in spec.file_name else ""
            guessed = _EXT_FALLBACK.get(ext) or mimetypes.guess_type(spec.file_name)[0]
        mime = (guessed or "").lower()

    mime = _MIME_ALIASES.get(mime, mime)

    if mime.startswith("text/"):
        # text/plain is the only text type Google reliably accepts; the rest
        # (code, csv, …) are plain text in practice anyway.
        return "text/plain"

    if mime in _ALLOWED_IMAGE or mime in _ALLOWED_DOC:
        return mime

    if mime.startswith("audio/") or mime.startswith("video/"):
        return mime

    if mime.startswith("image/"):
        # An unrecognised image format (gif or bmp, say): better not to try,
        # because Google's error would mean nothing to the user.
        raise MediaError(f"❌ فرمت تصویر پشتیبانی نمی‌شود ({mime}).")

    raise MediaError(f"❌ این نوع فایل پشتیبانی نمی‌شود ({mime or 'نامشخص'}).")


def _too_large(size: int, max_bytes: int) -> str:
    """The over-the-cap message, for a declared or a measured size."""
    return (f"❌ حجم فایل بیش از حد مجاز است "
            f"({size / 1048576:.1f} مگابایت؛ سقف {max_bytes / 1048576:.0f} مگابایت).")


async def _download_attempt(client, message) -> tuple[bytes | None, str]:
    """
    One download attempt. Returns `(data, why)`; data is None when it failed.

    `why` is an **English** one-liner for the log. MediaError carries the
    Persian sentence the user reads, and mixing the two would put a raw RPC
    name in front of them.
    """
    try:
        buffer = await client.download_media(message, in_memory=True)
    except FloodWait:
        # Deliberately not caught here: retrying straight into a flood wait
        # only earns a second one. download_bytes turns it into its own message.
        raise
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"[:200]

    if buffer is None:
        # download_media returns None for a transfer that was *stopped*, as
        # opposed to one that raised.
        return None, "download_media returned nothing"

    return (buffer.getvalue() if hasattr(buffer, "getvalue") else bytes(buffer)), ""


async def _refetch(client, message):
    """
    Re-read a message so that its media carries a fresh file reference.

    `messages.getMessages` marks nothing as read, so this is safe on any
    media, including a voice note the sender has not been told we played.
    `replies=0` stops it fetching the message's own reply as well.

    Returns None when the message cannot be re-read or came back without
    media — not an error in itself; the caller still has the first attempt's
    failure to report.
    """
    chat_id = getattr(getattr(message, "chat", None), "id", None)
    message_id = getattr(message, "id", None)

    if chat_id is None or message_id is None:
        return None

    try:
        fresh = await client.get_messages(chat_id, message_id, replies=0)
    except FloodWait:
        raise
    except Exception as e:
        log.info("could not re-read the message for a fresh file reference: %s",
                 type(e).__name__)
        return None

    if isinstance(fresh, list):
        fresh = fresh[0] if fresh else None

    # A deleted or unreachable message comes back as an empty placeholder
    # rather than as an error.
    if fresh is None or getattr(fresh, "empty", False):
        return None

    return fresh if inspect_media(fresh) is not None else None


async def download_bytes(client, message, max_bytes: int) -> bytes:
    """
    Download a message's media into memory, under a size cap.

    The size is checked *before* downloading, so a 500 MB video cannot eat
    memory; checking afterwards would defeat the purpose.

    A failed download is retried once against a **re-read** message, because
    the commonest reason for one is a stale file id rather than a broken
    transfer:

        A file id carries a `file_reference` that the server invalidates after
        a while, and Pyrogram serves `message.reply_to_message` straight out of
        `client.message_cache` (client.py:413, message.py:1875) — an object
        parsed when the message first arrived, holding whatever reference was
        valid *then*, for as long as the last thousand messages of that
        session. Replying to a voice note that has been sitting in the chat
        therefore reaches download_media with a reference the server has
        already dropped and fails with FILE_REFERENCE_EXPIRED, while the very
        same voice downloads fine once it is forwarded — the forwarded copy is
        a new message with a new reference.

    Re-reading mints a fresh reference and, because Message._parse writes it
    back into that same cache, also repairs the entry for the next command on
    the message. ttl_guard._refetch_media does the same thing one layer down
    for timed media.
    """
    spec = inspect_media(message)
    if spec is None:
        raise MediaError("❌ این پیام مدیایی ندارد.")

    if spec.size and spec.size > max_bytes:
        raise MediaError(_too_large(spec.size, max_bytes))

    try:
        data, why = await _download_attempt(client, message)

        if data is None:
            fresh = await _refetch(client, message)
            if fresh is None:
                why = f"{why}; re-read produced nothing to retry with"
            else:
                data, retry_why = await _download_attempt(client, fresh)
                if data is None:
                    why = f"{why}; after re-read: {retry_why}"
                else:
                    log.info("media downloaded on the second try, with a fresh "
                             "file reference")
    except FloodWait as e:
        log.warning("media download hit a FloodWait of %ss", getattr(e, "value", "?"))
        raise MediaError("❌ تلگرام موقتاً محدودیت گذاشته؛ کمی بعد دوباره امتحان کن.") from None

    if data is None:
        log.warning("media download failed: %s", why)
        raise MediaError("❌ دانلود فایل از تلگرام ناموفق بود.")

    # The real size may differ from the declared file_size, or not be declared.
    if len(data) > max_bytes:
        raise MediaError(_too_large(len(data), max_bytes))

    return data


# ==========================================================
#                     Audio conversions
# ==========================================================

def parse_pcm_rate(mime_type: str, default: int = 24000) -> int:
    """
    Read the sample rate out of the TTS output's mime.

    Google returns something like `audio/L16;codec=pcm;rate=24000`. Reading it
    from the response means that if the number ever changes, the voice is not
    silently sped up or slowed down.
    """
    for chunk in (mime_type or "").split(";"):
        chunk = chunk.strip().lower()
        if chunk.startswith("rate="):
            try:
                return int(chunk[5:])
            except ValueError:
                break
    return default


def pcm_to_wav(pcm: bytes, rate: int = 24000, channels: int = 1, sample_width: int = 2) -> bytes:
    """
    Wrap raw PCM in a WAV header.

    Google's TTS output is headerless PCM, which no player recognises.
    Building the 44-byte WAV header needs no library at all.
    """
    byte_rate = rate * channels * sample_width
    block_align = channels * sample_width
    header = (
        b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
        + b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, rate,
                                byte_rate, block_align, sample_width * 8)
        + b"data" + struct.pack("<I", len(pcm))
    )
    return header + pcm


def ffmpeg_path() -> str | None:
    """The path to ffmpeg, if it is installed on this system."""
    return shutil.which("ffmpeg")


async def pcm_to_ogg(pcm: bytes, rate: int = 24000) -> bytes | None:
    """
    Convert PCM to OGG/Opus with ffmpeg.

    Telegram only accepts a real voice message as OGG/Opus. With no ffmpeg
    installed this returns None and the caller falls back to a WAV file — so a
    missing ffmpeg does not remove the feature, it only changes how the result
    is presented.
    """
    binary = ffmpeg_path()
    if not binary:
        return None

    try:
        process = await asyncio.create_subprocess_exec(
            binary, "-hide_banner", "-loglevel", "error",
            "-f", "s16le", "-ar", str(rate), "-ac", "1", "-i", "pipe:0",
            "-c:a", "libopus", "-b:a", "32k", "-f", "ogg", "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(process.communicate(pcm), timeout=60)
    except (OSError, asyncio.TimeoutError) as e:
        log.warning("ffmpeg audio conversion failed: %s", type(e).__name__)
        return None

    if process.returncode != 0 or not out:
        log.warning("ffmpeg exited with code %s: %s", process.returncode, err[:200].decode(errors="ignore"))
        return None

    return out


def named_buffer(data: bytes, name: str) -> io.BytesIO:
    """
    Build an in-memory file for uploading through Pyrogram.

    Pyrogram needs a name attribute on a BytesIO to work out the file's name
    and extension; without it the upload is rejected.
    """
    buffer = io.BytesIO(data)
    buffer.name = name
    return buffer
