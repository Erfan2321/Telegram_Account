"""
Anti-deletion logger: caches private chat messages and reports deletions to Saved Messages.

Note:
    Both handlers intentionally omit @safe_handler because they intercept every private message;
    dispatching error responses into the chat would spam the recipient.
    Instead, internal try/except blocks are used and errors are solely logged.
"""

import asyncio
import html
import json
import logging
import os
import time

from pyrogram import Client, enums, filters
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait

import config
from utils import paths
from utils.db_manager import (
    MESSAGE_TTL, consume_ignored_deletion, get_message, save_message,
    set_local_media_path,
)
from utils.helpers import describe, flood_seconds, time_text

log = logging.getLogger(__name__)

# --- Message length budget ---
# Telegram caps messages at 4096 characters. Escaped HTML entities can significantly expand text size.
# Enforcing character budgets prevents MESSAGE_TOO_LONG errors and ensures deletion alerts are never dropped.
TEXT_BUDGET = 3500
SENDER_BUDGET = 100

# Maximum tolerable FloodWait delay in seconds for deletion reports
FLOOD_WAIT_LIMIT = 60

# Attributes carrying a file id, in the order Message stores them. The order
# matters for one case only: an animation is also a document, so whichever is
# checked first decides the label, and the more specific name has to win.
MEDIA_ATTRIBUTES = (
    "photo", "video_note", "voice", "sticker", "animation", "video",
    "audio", "document",
)

# Persian labels for the deletion report.
MEDIA_LABELS = {
    "photo": "عکس",
    "video": "ویدیو",
    "video_note": "ویدیو مسیج",
    "voice": "ویس",
    "sticker": "استیکر",
    "animation": "گیف",
    "audio": "فایل صوتی",
    "document": "فایل",
}

# Media that Telegram will not accept a caption on. Both are sent first and the
# report follows as its own message.
CAPTIONLESS = ("sticker", "video_note")

# Stickers are deliberately never downloaded. Their files are permanent, shared
# server-side objects rather than one-off uploads, so re-sending one by file id
# keeps working long after a photo's reference would have expired — spending
# disk on them buys nothing.
NEVER_DOWNLOAD = ("sticker",)

# What a media send attempt can come back with. Two successes have to be told
# apart: a sticker and a video note accept no caption, so the media arrives but
# the report still has to follow as its own message — and treating that as a
# failure would send the same media a second time from the local copy.
SENT = "sent"
NEEDS_TEXT = "needs-text"
FAILED = "failed"

# Concurrent pre-downloads. The work happens in the background so the cache
# handler stays fast, but a burst of media must not open a dozen simultaneous
# transfers on a connection that also carries the account's updates.
_DOWNLOAD_SLOTS = asyncio.Semaphore(3)

# Copies live exactly as long as the row that points at them (MESSAGE_TTL, 12
# hours): once the row is pruned there is nothing left to report about, so the
# file is dead weight.
CACHE_RETENTION = MESSAGE_TTL


def _describe_media(message):
    """
    Return (media_type, file_id) for a message, or (None, None) for plain text.

    The file id is what makes it possible to put the actual photo or voice back
    into Saved Messages when it is deleted; without it the report can only say
    that *something* was deleted.
    """
    for name in MEDIA_ATTRIBUTES:
        media = getattr(message, name, None)
        file_id = getattr(media, "file_id", None)
        if file_id:
            return name, file_id

    return None, None


def _media_meta(media) -> str:
    """
    The few numbers a re-upload needs to resemble the original.

    Only used on the fallback path, where the file has to be uploaded again
    from disk: Pyrogram's senders default duration and dimensions to zero, so
    without these a rescued voice arrives as a 0:00 clip and a video note as a
    1x1 circle — the same trap documented for the TTL guard (ARCHITECTURE.md §8.2).
    """
    meta = {
        key: value
        for key, value in (
            ("d", getattr(media, "duration", 0) or 0),
            ("w", getattr(media, "width", 0) or getattr(media, "length", 0) or 0),
            ("h", getattr(media, "height", 0) or getattr(media, "length", 0) or 0),
        )
        if value
    }
    return json.dumps(meta) if meta else None


def _sweep_cached_media() -> int:
    """
    Bound data/deleted_media/ by age and by total size. Returns files removed.

    Age alone is not a ceiling: twelve hours of photo-sharing can still fill a
    small disk, so once the total passes the configured cap the oldest files go
    too. Runs on write, like the TTL guard's sweep, because writing is the only
    thing that makes the directory grow.
    """
    cutoff = time.time() - CACHE_RETENTION
    removed = 0

    try:
        entries = [p for p in paths.DELETED_MEDIA_DIR.iterdir() if p.is_file()]
    except OSError:
        return 0

    survivors = []
    for entry in entries:
        try:
            stat = entry.stat()
            if stat.st_mtime < cutoff:
                entry.unlink()
                removed += 1
            else:
                survivors.append((stat.st_mtime, stat.st_size, entry))
        except OSError:
            # A file that vanished under us, or one we may not touch: neither
            # is worth interrupting the caller for.
            continue

    budget = config.DELETE_CACHE_TOTAL_MB * 1024 * 1024
    total = sum(size for _, size, _ in survivors)

    # Oldest first, so the newest copies — the ones most likely to be needed —
    # are the last to go.
    for _, size, entry in sorted(survivors):
        if total <= budget:
            break
        try:
            entry.unlink()
            total -= size
            removed += 1
        except OSError:
            continue

    if removed:
        log.info("swept %s cached media file(s)", removed)

    return removed


def _worth_caching(media, media_type) -> bool:
    """
    Whether this media earns a background download.

    Three exclusions, each for its own reason: stickers because their file ids
    outlive any reference expiry anyway; anything above the size cap because a
    large video is not worth the bandwidth on every private message; and timed
    media because the TTL guard has already downloaded it (ARCHITECTURE.md §8.2) and
    a second transfer would compete with the one that is racing the clock.
    """
    if not config.DELETE_CACHE_MEDIA or media is None:
        return False

    if media_type in NEVER_DOWNLOAD:
        return False

    if getattr(media, "ttl_seconds", None):
        return False

    size = getattr(media, "file_size", 0) or 0
    return 0 < size <= config.DELETE_CACHE_MAX_MB * 1024 * 1024


async def _cache_media_file(client, message, media, media_type):
    """
    Keep a copy of small media so a deletion report can survive an expired
    file reference.

    Runs as a background task: the row is already saved, the report only needs
    this if the message is deleted later, and a multi-megabyte download must
    not hold up the handler that caches the *next* message. Every failure is
    swallowed — this is a safety net, and a safety net that breaks the thing it
    protects is worse than none.
    """
    try:
        async with _DOWNLOAD_SLOTS:
            paths.DELETED_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
            _sweep_cached_media()

            # A fixed name per message: nothing else can collide with it, and a
            # re-delivered update simply overwrites its own file.
            target = paths.DELETED_MEDIA_DIR / f"{message.id}_{media_type}"
            path = await client.download_media(message, file_name=str(target))

        if not path or not os.path.exists(path):
            return

        await set_local_media_path(message.id, path)
        log.debug("cached %s for message %s", media_type, message.id)
    except Exception as e:
        log.warning("could not pre-cache a %s: %s", media_type, describe(e))


def _clock(stamp: float) -> str:
    """Format a timestamp for the report, in the configured display zone."""
    return time_text(stamp)


def _elapsed(seconds: float) -> str:
    """How long the message survived, in Persian and rounded to one unit."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} ثانیه"
    if seconds < 3600:
        return f"{seconds // 60} دقیقه"
    if seconds < 86400:
        return f"{seconds // 3600} ساعت و {(seconds % 3600) // 60} دقیقه"
    return f"{seconds // 86400} روز و {(seconds % 86400) // 3600} ساعت"


def _escape_clip(text: str, budget: int) -> str:
    """
    Escape HTML entities and truncate text to stay within the character budget.

    Truncation occurs after escaping to ensure payload limits are respected.
    Dangling/incomplete HTML entities created by slicing are stripped to avoid parse errors.
    """
    safe = html.escape(text or "")
    if len(safe) <= budget:
        return safe

    clipped = safe[:budget]
    last_amp = clipped.rfind("&")
    if last_amp != -1 and ";" not in clipped[last_amp:]:
        clipped = clipped[:last_amp]

    return clipped + "…"


# --- Dispatcher Group ---
# `filters.private` matches *every* private message, and Pyrogram's dispatcher breaks
# out of the handler loop after the first handler that runs inside a group
# (`pyrogram/dispatcher.py`, the `break` at the end of `for handler in group`).
# In group 0 this handler therefore swallowed every private message and silently
# disabled every command registered after it — plugins load alphabetically
# (`sorted(rglob("*.py"))`), so `.ttl` (ttl_guard) and `.stt`/`.tts` (voice_tools)
# came after `logger` and never ran in a PV at all.
#
# A dedicated positive group isolates the cache: commands dispatch first in group 0,
# then this always-matching passive handler runs on its own.
LOGGER_GROUP = 2


# 1. Cache private messages (both incoming and outgoing to detect two-way deletions)
@Client.on_message(filters.private, group=LOGGER_GROUP)
async def cache_private_messages(client, message):
    try:
        await _cache_one(client, message)
    except Exception as e:
        # Prevent database or runtime errors from bubbling up unhandled to the event loop
        log.warning("could not cache a private message: %s", describe(e))


async def _cache_one(client, message):
    # --- Exclusion 1: Bot chats ---
    # Checking chat type rather than sender flag ensures outgoing messages to bots are also ignored.
    if message.chat.type == enums.ChatType.BOT:
        return

    # Secondary check: incoming messages from bot service accounts
    if message.from_user and message.from_user.is_bot:
        return

    # --- Exclusion 2: Saved Messages (Self Chat) ---
    # Prevents an infinite feedback loop since deletion alerts are delivered to Saved Messages.
    me = getattr(client, "me", None)
    if me and message.chat.id == me.id:
        return

    is_outgoing = bool(message.outgoing)
    if is_outgoing:
        sender_name = "شما"
    else:
        sender_name = (message.from_user.first_name if message.from_user else None) or "ناشناس"

    media_type, file_id = _describe_media(message)

    # For media the stored text is only the caption. The kind used to be baked
    # into that text as "[فایل: PHOTO]", which was all the report could show;
    # now the kind lives in its own column and the file id lets the report send
    # the media itself, so the text stays what the person actually wrote.
    content_text = message.text or message.caption or ""

    # A message with neither text nor media (a service message, say) is not
    # worth a row — but media with no caption very much is.
    if not content_text and not file_id:
        return

    # Telegram's own date for the message, so the deletion report can show when
    # it was actually sent rather than when this process happened to see it —
    # the two diverge whenever the bot has been offline and is catching up.
    sent_at = message.date.timestamp() if getattr(message, "date", None) else None

    media = getattr(message, media_type, None) if media_type else None

    await save_message(
        message.id, message.chat.id, sender_name, content_text, is_outgoing,
        media_type=media_type, file_id=file_id, sent_at=sent_at,
        media_meta=_media_meta(media) if media else None,
    )

    if _worth_caching(media, media_type):
        # Deliberately not awaited: see _cache_media_file.
        asyncio.create_task(_cache_media_file(client, message, media, media_type))


# 2. Intercept and report deleted messages
@Client.on_deleted_messages()
async def catch_deleted_messages(client, messages):
    for msg in messages:
        try:
            await _report_one(client, msg)
        except Exception as e:
            # Isolate exceptions so a single malformed event does not interrupt batch processing
            log.warning("could not report a deleted message: %s", describe(e))


async def _report_one(client, msg):
    # --- Private chat filtering ---
    # In Pyrogram, channel/supergroup deletion events populate `msg.chat`, whereas private chats leave it None.
    # Discard non-private deletions to prevent ID collisions with group messages.
    if msg.chat is not None:
        return

    # --- Intentional self-deletions ---
    # Consume tracked deletion token; skips reporting if deleted via local commands (e.g. .del)
    if consume_ignored_deletion(msg.id):
        return

    cached_msg = await get_message(msg.id)
    if not cached_msg:
        return

    (_chat_id, sender_name, text, is_outgoing, cached_at,
     media_type, file_id, sent_at, local_path, media_meta) = cached_msg

    # Rows written before the sent_at column existed fall back to the cache
    # time, which for a running bot is the same instant anyway.
    sent_at = sent_at or cached_at

    # Use HTML formatting to safely escape untrusted text and prevent broken parse trees
    safe_text = _escape_clip(text, TEXT_BUDGET)
    safe_sender = _escape_clip(sender_name, SENDER_BUDGET)

    # Telegram's deletion update carries no timestamp of its own, so "now" is
    # as close to the moment of deletion as anything can be.
    deleted_at = time.time()
    when = (
        f"🕐 <b>زمان پیام:</b> {_clock(sent_at)}\n"
        f"🗓 <b>زمان حذف:</b> {_clock(deleted_at)}"
    )
    if sent_at:
        when += f" (بعد از {_elapsed(deleted_at - sent_at)})"

    # The body is deliberately *not* wrapped in <code>. That tag renders the text
    # monospace and tap-to-copy, which made every report look like a code block
    # instead of the message it is quoting. It was only ever decorative — what
    # keeps arbitrary user text from breaking the parse tree is html.escape()
    # in _escape_clip(), and that is untouched.
    if is_outgoing:
        # Outgoing message deletion (cannot distinguish initiator via Telegram MTProto event)
        report = "🗑 <b>پیام خروجی شما حذف شد:</b> (توسط شما یا مخاطب)\n\n"
    else:
        report = (
            "🗑 <b>پیام حذف شده در پی‌وی:</b>\n\n"
            f"👤 <b>فرستنده:</b> {safe_sender}\n"
        )

    if media_type:
        report += f"📎 <b>نوع:</b> {MEDIA_LABELS.get(media_type, media_type)}\n"

    report += when

    if safe_text:
        label = "کپشن" if media_type else "متن پیام"
        report += f"\n💬 <b>{label}:</b>\n{safe_text}"

    await _send_report(client, report, media_type, file_id, local_path, media_meta)


async def _send_media_report(client, report: str, media_type: str, file_id: str) -> str:
    """
    Put the deleted file back into Saved Messages from its stored file id.

    Nothing was downloaded when the message arrived, which is what keeps the
    cache cheap — but a file reference has a limited life and the message it
    belongs to is gone, so it cannot be refreshed. FAILED is therefore a normal
    outcome, not an error: the caller falls back to the local copy and then to
    the text report, which is exactly what this module did for every deletion
    before.
    """
    captionless = media_type in CAPTIONLESS

    try:
        await client.send_cached_media(
            "me", file_id,
            caption="" if captionless else report,
            parse_mode=ParseMode.HTML,
        )
    except FloodWait:
        # Left to the caller's own FloodWait handling, on the text report.
        return FAILED
    except Exception as e:
        log.warning(
            "could not re-send a deleted %s (%s); trying the local copy",
            media_type, describe(e),
        )
        return FAILED

    return NEEDS_TEXT if captionless else SENT


async def _upload_local_copy(client, report: str, media_type: str,
                             local_path: str, media_meta) -> str:
    """
    Re-upload the pre-downloaded copy.

    This is the route that survives an expired file reference, and it is the
    only one that has to re-declare the media's shape: uploading fresh bytes
    means Pyrogram's own defaults apply, and those defaults are zero duration
    and zero dimensions (`length=1` for a video note). The numbers come back
    out of media_meta so a rescued voice is not a 0:00 clip.
    """
    if not local_path or not os.path.exists(local_path):
        return FAILED

    try:
        meta = json.loads(media_meta) if media_meta else {}
    except (TypeError, ValueError):
        meta = {}

    duration, width, height = meta.get("d", 0), meta.get("w", 0), meta.get("h", 0)
    captionless = media_type in CAPTIONLESS
    caption = "" if captionless else report

    senders = {
        "photo": lambda: client.send_photo("me", local_path, caption=caption,
                                           parse_mode=ParseMode.HTML),
        "voice": lambda: client.send_voice("me", local_path, caption=caption,
                                           parse_mode=ParseMode.HTML, duration=duration),
        "video": lambda: client.send_video("me", local_path, caption=caption,
                                           parse_mode=ParseMode.HTML, duration=duration,
                                           width=width, height=height),
        "animation": lambda: client.send_animation("me", local_path, caption=caption,
                                                   parse_mode=ParseMode.HTML,
                                                   duration=duration, width=width,
                                                   height=height),
        "audio": lambda: client.send_audio("me", local_path, caption=caption,
                                           parse_mode=ParseMode.HTML, duration=duration),
        "video_note": lambda: client.send_video_note("me", local_path, duration=duration,
                                                     length=width or 384),
    }

    try:
        await senders.get(
            media_type,
            lambda: client.send_document("me", local_path, caption=caption,
                                         parse_mode=ParseMode.HTML),
        )()
    except FloodWait:
        return FAILED
    except Exception as e:
        log.warning(
            "could not re-upload the cached %s (%s); reporting as text instead",
            media_type, describe(e),
        )
        return FAILED

    log.info("re-sent a deleted %s from the local copy", media_type)
    return NEEDS_TEXT if captionless else SENT


async def _send_report(client, report: str, media_type=None, file_id=None,
                       local_path=None, media_meta=None):
    """
    Send the report to Saved Messages, with the media when that is possible.

    Order matters. The stored file id is tried first because it costs no upload
    and preserves the original exactly — a video note stays round, a voice
    keeps its waveform. Only when Telegram refuses it (an expired reference,
    typically) does the pre-downloaded copy get uploaded, and only then are the
    duration and dimensions re-declared by hand.
    """
    outcome = FAILED

    if file_id:
        outcome = await _send_media_report(client, report, media_type, file_id)

    if outcome == FAILED and local_path:
        outcome = await _upload_local_copy(client, report, media_type,
                                           local_path, media_meta)

    if outcome == SENT:
        return

    try:
        await client.send_message("me", report, parse_mode=ParseMode.HTML)
    except FloodWait as e:
        delay = flood_seconds(e)
        if delay > FLOOD_WAIT_LIMIT:
            log.warning("FloodWait %.0fs on the deletion report — dropped", delay)
            return
        await asyncio.sleep(delay)
        try:
            await client.send_message("me", report, parse_mode=ParseMode.HTML)
        except Exception as retry_error:
            log.warning("deletion report failed after FloodWait: %s", describe(retry_error))
    except Exception as e:
        log.warning("deletion report could not be sent: %s", describe(e))