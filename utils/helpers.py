"""
Shared handler utilities: safe responding, safe deleting, and a guard decorator.

The project-wide language rule:
    * Anything that goes to Telegram is **Persian** (the user reads it).
    * Anything that goes to a log or an exception is **English** (developers
      and bug reports read it, and it has to stay legible where right-to-left
      text would reorder itself).

This file is where those two worlds meet, so both kinds of text appear here;
everywhere else has only one of them.
"""

import asyncio
import functools
import html
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from pyrogram.enums import ParseMode
from pyrogram.errors import (
    BadRequest,
    FloodWait,
    MessageAuthorRequired,
    MessageDeleteForbidden,
    MessageIdInvalid,
    MessageNotModified,
    RPCError,
)

import config

log = logging.getLogger(__name__)

# Telegram's maximum message length.
TELEGRAM_LIMIT = 4096
# Safe boundary for splitting long messages (a little under the hard limit).
SAFE_LIMIT = 3900

# Ceiling on how long a handler will wait out a FloodWait (seconds).
#
# Telegram sometimes returns a FloodWait of several thousand seconds. Sleeping
# that long keeps a handler alive and useless for hours, which in practice is
# a leaked task. Beyond this ceiling the attempt is abandoned and only logged.
FLOOD_WAIT_LIMIT = 60

# Telegram rejects the whole request with MESSAGE_EMPTY when the text is
# empty. A visible placeholder beats that error.
EMPTY_PLACEHOLDER = "…"

# Appended when a response exceeds Telegram's limit and has to be cut.
TRUNCATED_NOTE = "\n\n… (متن بیش از حد طولانی بود و کوتاه شد)"


# Resolved once, on first use. A missing zone database must not take a working
# bot down over a cosmetic detail, so an unknown or unavailable zone degrades
# to the system clock and says so exactly once.
_display_zone = None
_display_zone_resolved = False


def _zone():
    """The configured display timezone, or None to use the system clock."""
    global _display_zone, _display_zone_resolved

    if not _display_zone_resolved:
        _display_zone_resolved = True
        name = getattr(config, "DISPLAY_TIMEZONE", "") or ""
        if name:
            try:
                _display_zone = ZoneInfo(name)
            except Exception as e:
                log.warning(
                    "unknown display timezone %r (%s); falling back to the system clock. "
                    "In Docker this usually means the tzdata package is missing.",
                    name, type(e).__name__,
                )

    return _display_zone


def local_time(stamp: float | None = None) -> datetime:
    """
    A POSIX timestamp (or now) as a datetime in the display timezone.

    Every user-facing time goes through here. `datetime.now()` and
    `datetime.fromtimestamp()` read the *process's* timezone, which inside a
    container is UTC — that is how a message sent at 21:30 came back reported
    as 18:00.
    """
    zone = _zone()

    if stamp is None:
        return datetime.now(zone) if zone else datetime.now()

    if zone:
        return datetime.fromtimestamp(stamp, timezone.utc).astimezone(zone)

    return datetime.fromtimestamp(stamp)


def time_text(stamp: float | None = None, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """Formatted local time, never raising on a nonsensical timestamp."""
    try:
        return local_time(stamp).strftime(fmt)
    except (OverflowError, OSError, ValueError, TypeError):
        return "؟"


def _first_username(user) -> str:
    """
    The account's @username, whichever of the two shapes Telegram used.

    A user carries either the single legacy `username` or — since collectible
    usernames — a list in `usernames`, where only the active entries actually
    resolve. The raw MTProto object and Pyrogram's parsed one agree on both
    field names, so one reader serves the TTL guard and the deletion logger
    alike.
    """
    single = getattr(user, "username", None)
    if single:
        return str(single)

    for entry in getattr(user, "usernames", None) or []:
        candidate = getattr(entry, "username", None)
        if candidate and getattr(entry, "active", True):
            return str(candidate)

    return ""


def contact_lines(user, user_id=None) -> str:
    """
    How to identify the other party in a report: @username and phone number.

    Reports about a private chat name the person, and a display name alone is
    not an identity — two contacts share one, and a person who renames himself
    after the fact leaves the report pointing at nobody. The username and the
    phone number are what stay pinned to the account.

    Neither is guaranteed to exist: an account can publish no username, and a
    phone number is only visible for a saved contact. When both are missing the
    numeric id is written in their place, because it is always known — a
    deletion report reaches this function holding the chat id even when the
    peer itself could not be resolved.

    Returned as ready-to-send **HTML** lines, escaped: `user` comes from the
    other side of the conversation and is untrusted (§10.8). The phone and the
    numeric id sit in <code> so they can be tapped to copy; the username is
    left plain so Telegram links it to the chat itself.
    """
    lines = []

    username = _first_username(user)
    if username:
        lines.append(f"🆔 <b>آیدی:</b> @{html.escape(username)}")

    # Pyrogram renames the raw `phone` field to `phone_number`; either object
    # can arrive here.
    phone = getattr(user, "phone_number", None) or getattr(user, "phone", None)
    if phone:
        # Telegram stores the number bare. The leading + is stripped before it
        # is added back so a value that already carries one is not doubled.
        digits = html.escape(str(phone).strip().lstrip("+"))
        if digits:
            lines.append(f"📞 <b>شماره:</b> <code>+{digits}</code>")

    if not lines:
        numeric = getattr(user, "id", None) or user_id
        lines.append(
            f"🆔 <b>آیدی عددی:</b> <code>{html.escape(str(numeric))}</code>"
            if numeric else "🆔 <b>آیدی عددی:</b> ؟"
        )

    return "\n".join(lines)


def describe(error: BaseException, limit: int = 160) -> str:
    """
    A one-line **English** summary of an exception, for the log.

    The class name is always included, because many Pyrogram errors carry an
    empty message and without it the log line would say nothing at all.
    """
    text = str(error).replace("\n", " ").strip()
    name = type(error).__name__
    if not text:
        return name
    if len(text) > limit:
        text = text[:limit] + "…"
    return f"{name}: {text}"


def _clamp(text: str) -> str:
    """Clamp text to Telegram's limit and substitute a placeholder when empty."""
    if not text:
        return EMPTY_PLACEHOLDER
    if len(text) > TELEGRAM_LIMIT:
        return text[:SAFE_LIMIT] + TRUNCATED_NOTE
    return text


def flood_seconds(error: BaseException, default: float = 0.0) -> float:
    """
    A FloodWait's delay, as a number that is always usable.

    This helper is needed because Pyrogram's `.value` is not guaranteed to be
    numeric: RPCError.__init__ tries `int(value)` first and, failing that,
    keeps the raw value — and when Telegram sends no number at all it stays
    None (`FloodWait().value is None`). So `asyncio.sleep(e.value)` can raise
    a TypeError of its own, *inside* the error-handling path, where a fresh
    exception is the worst possible outcome.
    """
    raw = getattr(error, "value", None)
    try:
        delay = float(raw)
    except (TypeError, ValueError):
        return default
    if delay != delay or delay < 0:  # NaN or negative
        return default
    return delay


async def _sleep_for_flood(error: FloodWait, where: str) -> bool:
    """
    Sleep out a FloodWait, subject to the ceiling.

    True means we waited and retrying makes sense; False means the requested
    delay was too long to be worth waiting for.
    """
    delay = flood_seconds(error)
    if delay > FLOOD_WAIT_LIMIT:
        log.warning("FloodWait %.0fs in %s — too long to wait, giving up", delay, where)
        return False

    log.warning("FloodWait %.0fs in %s — waiting", delay, where)
    await asyncio.sleep(delay)
    return True


async def _deliver(message, text: str, prefer_edit: bool, kwargs: dict):
    """One real delivery attempt: edit our own message, or reply."""
    if prefer_edit:
        return await message.edit_text(text, **kwargs)
    return await message.reply_text(text, **kwargs)


async def respond(message, text: str, **kwargs):
    """
    Respond to a message, picking edit or reply automatically.

    If the message belongs to this account (outgoing) it is edited; otherwise
    the response is sent as a reply to it.

    Why this exists: AUTH_FILTER lets authorized users run commands too, but
    their messages are incoming, and edit_text on someone else's message fails
    with MESSAGE_AUTHOR_REQUIRED.

    This function **never raises**. If the text cannot be delivered in any
    form it returns None and logs the cause; callers account for that with the
    `status or message` pattern throughout. If responding could raise, the
    safe_handler decorator — which calls this very function to report errors —
    would end up in an error loop.
    """
    text = _clamp(text)
    prefer_edit = bool(getattr(message, "outgoing", False))

    try:
        return await _deliver(message, text, prefer_edit, kwargs)

    except MessageNotModified:
        # The new text matched the old one; nothing further to do.
        return message

    except FloodWait as e:
        if not await _sleep_for_flood(e, "respond"):
            return None
        try:
            return await _deliver(message, text, prefer_edit, kwargs)
        except MessageNotModified:
            return message
        except Exception as retry_error:
            log.warning("respond failed after FloodWait: %s", describe(retry_error))
            return None

    except (MessageIdInvalid, MessageAuthorRequired) as e:
        # The anchor message is gone or is not ours. A fresh reply is the only
        # remaining option.
        log.info("cannot edit anchor message (%s); replying instead", type(e).__name__)
        return await _reply_fallback(message, text, kwargs)

    except BadRequest as e:
        # Usually a Markdown/entity parse error: the text we handed Telegram
        # was not valid as formatting. Send it once more with no parser at
        # all — raw text always beats no message.
        return await _plain_fallback(message, text, prefer_edit, kwargs, e)

    except RPCError as e:
        log.warning("respond failed: %s", describe(e))
        return None

    except Exception as e:
        # Non-RPC failures have to be contained here too: a dropped socket, a
        # timeout, or a bug one layer down. The "respond never raises"
        # guarantee has to be *genuinely* unconditional, because safe_handler
        # calls this same function to report errors — an escaping exception
        # here would be an error inside the error path.
        log.warning("respond failed unexpectedly: %s", describe(e))
        return None


async def _plain_fallback(message, text: str, prefer_edit: bool, kwargs: dict, cause):
    """Retry with no formatting at all, after Telegram rejected the formatted text."""
    if kwargs.get("parse_mode") is ParseMode.DISABLED:
        # It was already sent without a parser, so the cause is something else.
        log.warning("respond rejected by Telegram: %s", describe(cause))
        return None

    log.warning("respond rejected (%s); retrying with parsing disabled", describe(cause))
    retry_kwargs = dict(kwargs)
    retry_kwargs["parse_mode"] = ParseMode.DISABLED

    try:
        return await _deliver(message, text, prefer_edit, retry_kwargs)
    except MessageNotModified:
        return message
    except Exception as e:
        log.warning("respond failed even without parsing: %s", describe(e))
        return None


async def _reply_fallback(message, text: str, kwargs: dict):
    """Last resort: reply, when editing is not possible."""
    try:
        return await message.reply_text(text, **kwargs)
    except Exception as e:
        log.warning("reply fallback failed: %s", describe(e))
        return None


async def respond_long(message, text: str, **kwargs):
    """
    Respond with text longer than Telegram's limit.

    The first chunk takes the place of the normal response (edit or reply) and
    the rest follow it. respond() on its own would truncate long text, which
    for a translation or a transcript means losing part of the result.

    Like respond it never raises; if the continuation messages fail to send,
    what did arrive is kept and the failure is only logged.
    """
    chunks = split_text(text)
    first = await respond(message, chunks[0], **kwargs)
    anchor = first if first is not None else message

    for index, chunk in enumerate(chunks[1:], start=2):
        try:
            anchor = await anchor.reply_text(chunk, quote=False, **kwargs)
        except FloodWait as e:
            if not await _sleep_for_flood(e, "respond_long"):
                break
            try:
                anchor = await anchor.reply_text(chunk, quote=False, **kwargs)
            except Exception as retry_error:
                log.warning("chunk %d dropped after FloodWait: %s",
                            index, describe(retry_error))
                break
        except Exception as e:
            log.warning("chunk %d of %d could not be sent: %s",
                        index, len(chunks), describe(e))
            break

    return first


async def safe_delete(client, message) -> bool:
    """
    Delete one of our own messages without tripping the anti-delete alert.

    The anti-delete handler does not know who deleted a message, so removing a
    status message ("⏳ processing") used to produce a spurious deletion
    report. As with the .del command, the id is marked before the deletion —
    private chats only, since the logger caches nothing else.
    """
    if message is None:
        return False

    # The in-function import is deliberate: db_manager depends on paths, and
    # helpers is imported from all over the project (including modules that
    # need no database), so this also removes a potential import cycle.
    from pyrogram import enums

    from utils.db_manager import mark_deletions_ignored

    try:
        if getattr(message.chat, "type", None) == enums.ChatType.PRIVATE:
            mark_deletions_ignored([message.id])

        await message.delete()
        return True
    except (MessageDeleteForbidden, MessageIdInvalid) as e:
        log.info("status message could not be deleted: %s", type(e).__name__)
        return False
    except Exception as e:
        log.warning("status message could not be deleted: %s", describe(e))
        return False


def safe_handler(func):
    """
    Guard decorator for Pyrogram handlers.

    Catches any unexpected error, logs it with a full traceback and English
    text, and shows the user a short Persian message — instead of letting the
    handler die silently inside Pyrogram's dispatcher.

    Only put it on *commands*. Handlers that run on every message (the logger,
    auto-seen, global-list enforcement) deliberately omit it: their error
    message would be posted into the other party's chat, once per message.
    """

    @functools.wraps(func)
    async def wrapper(client, message, *args, **kwargs):
        try:
            return await func(client, message, *args, **kwargs)

        except asyncio.CancelledError:
            # A deliberate cancellation (during shutdown, say) is not an error.
            raise

        except FloodWait as e:
            await _sleep_for_flood(e, f"handler {func.__name__}")

        except Exception as e:
            log.exception("unhandled error in handler %s", func.__name__)
            await _report_handler_error(message, e)

    return wrapper


async def _report_handler_error(message, error: BaseException) -> None:
    """
    Show a handler error to the user.

    The error text is uncontrolled (it may contain backticks or asterisks), so
    HTML + escaping is used to keep Telegram from rejecting the whole message.
    The explanatory sentence is Persian and the technical detail English — the
    usual split.
    """
    detail = html.escape(describe(error))
    text = f"❌ خطای غیرمنتظره در اجرای دستور:\n<code>{detail}</code>"

    if await respond(message, text, parse_mode=ParseMode.HTML) is None:
        log.error("could not report the handler error to the user")


def split_text(text: str, limit: int = SAFE_LIMIT):
    """
    Split long text into chunks below Telegram's limit.

    Tries to cut on a newline so the text stays readable.
    """
    if not text:
        return [EMPTY_PLACEHOLDER]

    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = text.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        parts.append(text)
    return parts
