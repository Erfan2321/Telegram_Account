"""
Group member management — kick, ban, mute, and their global variants.

Three layers:

  1. Single-group commands (`.kick`, `.ban`, `.unban`, `.mute`, `.unmute`)
     are executed in the specific group where the command is issued.

  2. Global commands (`.gban`, `.gmute`, and their reverses) apply restrictions
     to the user across *all* groups where this account has admin privileges,
     and store the user's ID in the database.

  3. Auto-enforcement handler: If a globally restricted user later sends a
     message in a managed group, the restriction is enforced immediately
     (`GBAN_ENFORCE`).

Scope Warning: This module operates on a real user account (Userbot). Global
operations send dozens of sequential requests to Telegram; bulk behavior is a
primary trigger for account limitations (FloodWait/Ban). The delay between
requests (`GLOBAL_ACTION_DELAY`) exists for this specific reason. Decreasing
it directly increases the risk of account suspension.
"""

import asyncio
import html
import inspect
import logging
import time
from datetime import datetime, timedelta

from pyrogram import Client, enums, filters, raw, utils
from pyrogram.enums import ParseMode
from pyrogram.errors import (
    ChannelPrivate,
    ChatAdminRequired,
    ChatWriteForbidden,
    FloodWait,
    PeerIdInvalid,
    PeerIdNotSupported,
    RightForbidden,
    UserAdminInvalid,
    UserCreator,
    UserIdInvalid,
    UserNotParticipant,
    UsernameInvalid,
    UsernameNotOccupied,
)
from pyrogram.types import ChatPermissions

import config
from utils.db_manager import (
    GBANNED_USERS,
    GMUTED_USERS,
    add_global_flag,
    get_global_flags,
    remove_global_flag,
)
from utils.helpers import flood_seconds, respond, safe_handler

log = logging.getLogger(__name__)

# AUTH_FILTER is imported from the general plugin to avoid redefining access logic.
# This ensures that if access rules change, this module remains synchronized.
# The plugin loader registers this module under `plugins.general`, so the import
# returns the exact same object, preventing duplicate handler registration.
from plugins.general import AUTH_FILTER  # noqa: E402

PREFIXES = ["/", "."]

# --- Dispatcher group for the auto-enforcement handler ---
# In Pyrogram's dispatcher (pyrogram/dispatcher.py), the event loop breaks after
# the first executed handler *within each group*. Therefore, this handler needs
# its own dedicated group:
#   * Group 0: Consumes commands and messages before `general/auto_seen`.
#   * Group -1: Reserved for the raw `ttl_guard` handler (picks up updates unconditionally).
# Because plugins are loaded alphabetically, this module registers before `ttl_guard`.
# Placing it in Group -2 ensures it runs without interfering with the expiring media guard.
GBAN_ENFORCE_GROUP = -2

# --- Chat types where member management is applicable ---
GROUP_TYPES = (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP)

# --- Telegram Time Limits ---
# The Telegram server interprets any restriction duration less than 30 seconds
# or more than 366 days as "forever". A user executing `.mute 10s` does not
# expect a permanent mute, so we reject out-of-bounds inputs instead of executing them silently.
MIN_RESTRICT_SECONDS = 30
MAX_RESTRICT_SECONDS = 366 * 86400

# Mapping duration suffixes to seconds (supports both English and Persian characters)
_DURATION_UNITS = {
    "s": 1, "ث": 1,
    "m": 60, "د": 60,
    "h": 3600, "س": 3600,
    "d": 86400, "ر": 86400,
    "w": 604800, "ه": 604800,
}

# --- Dialog Pagination Settings ---
DIALOG_PAGE = 100          # Telegram's maximum limit per GetDialogs request
MAX_DIALOG_PAGES = 60      # Hard limit for pagination: 6000 dialogs, more than enough
MANAGED_CACHE_TTL = 300    # Cache validity duration for managed groups list (seconds)

# --- Global Operations Progress Settings ---
PROGRESS_EVERY = 8         # Update status message every N groups
MAX_LIST_ROWS = 30         # Maximum rows displayed in .gbanlist / .gmutelist

# --- Mute Access Permission Sets ---
# ChatPermissions() defaults all fields to None. restrict_chat_member translates
# None to `not permissions.can_*` (restricted). Result: Complete silence.
MUTED_PERMS = ChatPermissions()

# Setting everything to True removes user-specific restrictions, reverting them
# to the group's default permissions. It does not grant absolute freedom.
#
# The list is built from ChatPermissions' own signature rather than written out,
# because it grew from 8 fields to 17 (separate audio/photo/video/voice-note
# rights, reactions, topics). ChatPermissions.write() bans every field it finds
# unset, so a hardcoded list would leave each newly added right *restricted*
# after .unmute — a silent half-unmute that nothing would report.
#
# can_send_media_messages is deliberately excluded: it is a deprecated alias
# that, when set, blanks the granular send_* rights and logs a warning on every
# call.
UNMUTED_PERMS = ChatPermissions(**{
    name: True
    for name in inspect.signature(ChatPermissions.__init__).parameters
    if name.startswith("can_") and name != "can_send_media_messages"
})

# --- Managed Groups Cache ---
# Structure: (monotonic_time, [ (chat_id, title, is_supergroup), ... ])
# We use time.monotonic() instead of time.time() to prevent system clock jumps
# from permanently invalidating or validating the cache (refer to utils/clock.py).
_managed_cache = None
_managed_cache_at = 0.0

# --- Auto-Enforcement Anti-Spam Cache ---
# Structure: {(chat_id, user_id): monotonic_time}
# Prevents a listed user's messages in unmanaged groups from triggering
# consecutive failed API requests.
_ENFORCED = {}
_ENFORCED_TTL = 900

# Simple statistics for status commands
_STATS = {"enforced": 0, "failed": 0}

# Maximum wait time for FloodWait (seconds). A global operation spanning dozens
# of groups should not sleep for hours due to a single rate-limit error.
FLOOD_WAIT_LIMIT = 60


# ==========================================================
#                        Helpers
# ==========================================================

def _parse_duration(token: str):
    """
    Parses duration strings like "10m", "2h", or "1d" into seconds.

    Returns: The total seconds as an integer, or None if the token does not
    match a duration format (implying the token is likely part of the reason string).
    """
    if not token:
        return None

    unit = token[-1].lower()
    if unit not in _DURATION_UNITS:
        return None

    try:
        amount = int(token[:-1])
    except ValueError:
        return None

    if amount <= 0:
        return None

    return amount * _DURATION_UNITS[unit]


def _format_duration(seconds: int) -> str:
    """Formats duration in seconds into a human-readable Persian string for reporting."""
    if seconds >= 86400:
        return f"{seconds // 86400} روز"
    if seconds >= 3600:
        return f"{seconds // 3600} ساعت"
    if seconds >= 60:
        return f"{seconds // 60} دقیقه"
    return f"{seconds} ثانیه"


def _display_name(user) -> str:
    """Returns a non-empty display name for a given user."""
    if user is None:
        return "ناشناس"

    first = getattr(user, "first_name", "") or ""
    last = getattr(user, "last_name", "") or ""
    full = (first + " " + last).strip()
    if full:
        return full

    username = getattr(user, "username", "") or ""
    if username:
        return "@" + username

    return str(getattr(user, "id", "ناشناس"))


def _friendly_error(error) -> str:
    """
    Translates Pyrogram exceptions into actionable Persian error messages.

    The goal is to inform the user exactly *what* went wrong and how to fix it.
    Unmapped errors return the exception class name to keep logs traceable.
    """
    if isinstance(error, ChatAdminRequired):
        return "در این گروه دسترسی ادمین نداری."
    if isinstance(error, RightForbidden):
        return "ادمین هستی ولی اختیار «محدود کردن اعضا» به تو داده نشده."
    if isinstance(error, UserAdminInvalid):
        return "این کاربر ادمین است؛ اول باید مقامش برداشته شود."
    if isinstance(error, UserCreator):
        return "این کاربر مالک گروه است و قابل محدود کردن نیست."
    if isinstance(error, UserNotParticipant):
        return "این کاربر عضو این گروه نیست."
    if isinstance(error, (PeerIdInvalid, PeerIdNotSupported, UserIdInvalid)):
        return ("کاربر شناخته نشد. روی یکی از پیام‌هایش ریپلای کن تا "
                "آیدی‌اش در سشن ذخیره شود.")
    if isinstance(error, (UsernameNotOccupied, UsernameInvalid)):
        return "چنین یوزرنیمی وجود ندارد."
    if isinstance(error, (ChannelPrivate, ChatWriteForbidden)):
        return "دسترسی به این گروه از دست رفته است."

    return f"خطای تلگرام: {type(error).__name__}"


async def _resolve_target(client, message):
    """
    Resolves the target user and the remaining command arguments.

    Returns: (user_id, display_name, remaining_args, error_message)
    If successful, error_message is None.

    Priority is given to replies. If the command replies to a message, all tokens
    following the command are treated as "reason/duration" rather than a target user.
    """
    args = list(message.command[1:])

    # --- Case 1: Reply ---
    reply = message.reply_to_message
    if reply is not None:
        if reply.from_user is not None:
            return reply.from_user.id, _display_name(reply.from_user), args, None

        # Message is from a channel/group (anonymous admin or channel linked post)
        if getattr(reply, "sender_chat", None) is not None:
            return None, "", args, "❌ این پیام از طرف یک کانال فرستاده شده، نه یک کاربر."

        return None, "", args, "❌ فرستنده این پیام مشخص نیست (پیام سرویسی یا ناشناس)."

    # --- Case 2: Explicit Argument ---
    if not args:
        return None, "", args, None  # Let the caller handle showing the help text

    spec = args[0]
    rest = args[1:]

    # Parse numerical IDs directly. If the user isn't cached, get_users will fail,
    # but we still retain the numeric ID to attempt the operation anyway.
    numeric = None
    try:
        numeric = int(spec)
    except ValueError:
        pass

    try:
        user = await client.get_users(numeric if numeric is not None else spec)
        return user.id, _display_name(user), rest, None
    except Exception as e:
        if numeric is not None:
            # ID is valid but profile is inaccessible. We can still try the operation
            # (e.g., the user might be cached internally in the current chat).
            log.info("could not fetch profile for user %s: %s", numeric, type(e).__name__)
            return numeric, str(numeric), rest, None

        return None, "", rest, f"❌ {_friendly_error(e)}"


def _split_duration_and_reason(args):
    """
    Separates the optional duration token from the reason string.

    Returns: (seconds or None, reason_string, error_message or None)

    Only the *first* token is evaluated as a duration. If it doesn't match
    the duration format, the entire remaining array is treated as the reason.
    """
    if not args:
        return None, "", None

    seconds = _parse_duration(args[0])
    if seconds is None:
        return None, " ".join(args).strip(), None

    reason = " ".join(args[1:]).strip()

    if seconds < MIN_RESTRICT_SECONDS:
        return None, reason, (
            f"❌ کمتر از {MIN_RESTRICT_SECONDS} ثانیه از نظر تلگرام یعنی «برای همیشه». "
            "مدت بیشتری بده یا اصلا مدت ننویس."
        )
    if seconds > MAX_RESTRICT_SECONDS:
        return None, reason, (
            "❌ بیشتر از ۳۶۶ روز از نظر تلگرام یعنی «برای همیشه». "
            "برای دائمی بودن، مدت را حذف کن."
        )

    return seconds, reason, None


def _until_kwargs(seconds):
    """
    Constructs the until_date kwarg for Pyrogram API methods.

    If no duration is provided, an empty dict is returned, preserving
    Pyrogram's default behavior (epoch = permanent restriction).
    """
    if not seconds:
        return {}
    return {"until_date": datetime.now() + timedelta(seconds=seconds)}


def _guard_chat(message) -> str:
    """Validates that the command was executed in a valid chat type."""
    if message.chat.type not in GROUP_TYPES:
        return "❌ این دستور فقط داخل گروه کار می‌کند."
    return ""


def _guard_self(client, user_id: int) -> str:
    """Prevents the userbot from executing restrictive commands on itself."""
    me = getattr(client, "me", None)
    if me is not None and user_id == me.id:
        return "❌ روی خودت که نمی‌شود."
    return ""


# ==========================================================
#                  Single-Group Operations
# ==========================================================

async def _ban(client, chat_id, user_id, seconds=None):
    await client.ban_chat_member(chat_id, user_id, **_until_kwargs(seconds))


async def _unban(client, chat_id, user_id):
    await client.unban_chat_member(chat_id, user_id)


async def _mute(client, chat_id, user_id, seconds=None):
    await client.restrict_chat_member(chat_id, user_id, MUTED_PERMS, **_until_kwargs(seconds))


async def _unmute(client, chat_id, user_id):
    await client.restrict_chat_member(chat_id, user_id, UNMUTED_PERMS)


# ==========================================================
#                  Single-Group Commands
# ==========================================================

@Client.on_message(AUTH_FILTER & filters.command("kick", prefixes=PREFIXES))
@safe_handler
async def kick_command(client, message):
    """
    Kicks a user without banning them (allowing them to rejoin).

    Telegram lacks a dedicated 'kick' method; it is achieved by banning and
    immediately unbanning the user. In basic groups, `ban_chat_member` routes
    to `messages.DeleteChatUser` which acts purely as a removal tool, making
    the unban step unnecessary (and unsupported) in that context.
    """
    chat_error = _guard_chat(message)
    if chat_error:
        await respond(message, chat_error)
        return

    user_id, name, args, error = await _resolve_target(client, message)
    if error:
        await respond(message, error)
        return
    if user_id is None:
        await respond(message, "❌ روی پیام کاربر ریپلای کن یا آیدی/یوزرنیمش را بنویس.\n"
                               "مثال: `.kick @username اسپم`")
        return

    self_error = _guard_self(client, user_id)
    if self_error:
        await respond(message, self_error)
        return

    reason = " ".join(args).strip()
    is_supergroup = message.chat.type == enums.ChatType.SUPERGROUP

    try:
        await _ban(client, message.chat.id, user_id)
        if is_supergroup:
            # Without this, the 'kick' effectively becomes a permanent ban.
            await _unban(client, message.chat.id, user_id)
    except Exception as e:
        log.warning("kick of %s in chat %s failed: %s", user_id, message.chat.id, type(e).__name__)
        await respond(message, f"❌ {_friendly_error(e)}")
        return

    await _report(message, "👢 <b>اخراج شد</b>", name, user_id, reason)


@Client.on_message(AUTH_FILTER & filters.command("ban", prefixes=PREFIXES))
@safe_handler
async def ban_command(client, message):
    """Bans a user in the current group, with an optional duration (e.g., `.ban 2h spam`)."""
    chat_error = _guard_chat(message)
    if chat_error:
        await respond(message, chat_error)
        return

    user_id, name, args, error = await _resolve_target(client, message)
    if error:
        await respond(message, error)
        return
    if user_id is None:
        await respond(message, "❌ روی پیام کاربر ریپلای کن یا آیدی/یوزرنیمش را بنویس.\n"
                               "مثال: `.ban @username 2h اسپم`")
        return

    self_error = _guard_self(client, user_id)
    if self_error:
        await respond(message, self_error)
        return

    seconds, reason, duration_error = _split_duration_and_reason(args)
    if duration_error:
        await respond(message, duration_error)
        return

    # In a basic group, temporary bans are not supported. Pyrogram maps this to
    # DeleteChatUser, which doesn't accept an until_date parameter.
    if seconds and message.chat.type == enums.ChatType.GROUP:
        await respond(message, "❌ مسدودی مدت‌دار فقط در سوپرگروه ممکن است.")
        return

    try:
        await _ban(client, message.chat.id, user_id, seconds)
    except Exception as e:
        log.warning("ban of %s in chat %s failed: %s", user_id, message.chat.id, type(e).__name__)
        await respond(message, f"❌ {_friendly_error(e)}")
        return

    extra = f"⏱ <b>مدت:</b> {_format_duration(seconds)}" if seconds else ""
    await _report(message, "🚫 <b>مسدود شد</b>", name, user_id, reason, extra)


@Client.on_message(AUTH_FILTER & filters.command("unban", prefixes=PREFIXES))
@safe_handler
async def unban_command(client, message):
    """Unbans a user in the current group. The user is not auto-added but can rejoin."""
    chat_error = _guard_chat(message)
    if chat_error:
        await respond(message, chat_error)
        return

    # Unbanning routes through channels.EditBanned. Basic groups are not channels
    # and lack a ban list entirely.
    if message.chat.type == enums.ChatType.GROUP:
        await respond(message, "❌ رفع مسدودی فقط در سوپرگروه ممکن است "
                               "(گروه ساده اصلا لیست مسدودی ندارد).")
        return

    user_id, name, args, error = await _resolve_target(client, message)
    if error:
        await respond(message, error)
        return
    if user_id is None:
        await respond(message, "❌ روی پیام کاربر ریپلای کن یا آیدی/یوزرنیمش را بنویس.\n"
                               "مثال: `.unban 123456789`")
        return

    try:
        await _unban(client, message.chat.id, user_id)
    except Exception as e:
        log.warning("unban of %s in chat %s failed: %s", user_id, message.chat.id, type(e).__name__)
        await respond(message, f"❌ {_friendly_error(e)}")
        return

    await _report(message, "♻️ <b>رفع مسدودی شد</b>", name, user_id, " ".join(args).strip())


@Client.on_message(AUTH_FILTER & filters.command("mute", prefixes=PREFIXES))
@safe_handler
async def mute_command(client, message):
    """Mutes a user in the current group, with an optional duration (e.g., `.mute 30m`)."""
    chat_error = _guard_chat(message)
    if chat_error:
        await respond(message, chat_error)
        return

    # Basic groups do not support member-specific restrictions.
    if message.chat.type == enums.ChatType.GROUP:
        await respond(message, "❌ بی‌صدا کردن فقط در سوپرگروه ممکن است. "
                               "گروه ساده محدودیت جداگانه برای هر عضو ندارد.")
        return

    user_id, name, args, error = await _resolve_target(client, message)
    if error:
        await respond(message, error)
        return
    if user_id is None:
        await respond(message, "❌ روی پیام کاربر ریپلای کن یا آیدی/یوزرنیمش را بنویس.\n"
                               "مثال: `.mute @username 30m`")
        return

    self_error = _guard_self(client, user_id)
    if self_error:
        await respond(message, self_error)
        return

    seconds, reason, duration_error = _split_duration_and_reason(args)
    if duration_error:
        await respond(message, duration_error)
        return

    try:
        await _mute(client, message.chat.id, user_id, seconds)
    except Exception as e:
        log.warning("mute of %s in chat %s failed: %s", user_id, message.chat.id, type(e).__name__)
        await respond(message, f"❌ {_friendly_error(e)}")
        return

    extra = f"⏱ <b>مدت:</b> {_format_duration(seconds)}" if seconds else ""
    await _report(message, "🔇 <b>بی‌صدا شد</b>", name, user_id, reason, extra)


@Client.on_message(AUTH_FILTER & filters.command("unmute", prefixes=PREFIXES))
@safe_handler
async def unmute_command(client, message):
    """Removes a user's restrictions in the current group."""
    chat_error = _guard_chat(message)
    if chat_error:
        await respond(message, chat_error)
        return

    if message.chat.type == enums.ChatType.GROUP:
        await respond(message, "❌ این دستور فقط در سوپرگروه ممکن است.")
        return

    user_id, name, args, error = await _resolve_target(client, message)
    if error:
        await respond(message, error)
        return
    if user_id is None:
        await respond(message, "❌ روی پیام کاربر ریپلای کن یا آیدی/یوزرنیمش را بنویس.\n"
                               "مثال: `.unmute @username`")
        return

    try:
        await _unmute(client, message.chat.id, user_id)
    except Exception as e:
        log.warning("unmute of %s in chat %s failed: %s", user_id, message.chat.id, type(e).__name__)
        await respond(message, f"❌ {_friendly_error(e)}")
        return

    await _report(message, "🔊 <b>محدودیت برداشته شد</b>", name, user_id, " ".join(args).strip())


async def _report(message, title: str, name: str, user_id: int, reason: str, extra: str = ""):
    """
    Standardized reporting for the result of a single-group action.

    HTML and html.escape are used following rule §10.6: The display name and reason
    are both untrusted inputs. An unescaped `_` or `*` in Markdown would either break
    the rendering or cause Telegram to reject the entire message.
    """
    lines = [
        title,
        "",
        f"👤 <b>کاربر:</b> {html.escape(name)}",
        f"🆔 <code>{user_id}</code>",
    ]
    if extra:
        lines.append(extra)
    if reason:
        lines.append(f"📝 <b>دلیل:</b> {html.escape(reason)}")

    await respond(message, "\n".join(lines), parse_mode=ParseMode.HTML)


# ==========================================================
#                  Finding Managed Groups
# ==========================================================

def _admin_group_from_raw(chat):
    """
    Do we have member restriction privileges in this raw chat?

    Returns: (chat_id, title, is_supergroup) or None.

    Why use the raw layer instead of client.get_dialogs()?
        Pyrogram's high-level Chat type only retains `is_creator` and discards
        `admin_rights` (see Chat._parse_channel_chat). Using `get_dialogs` would force
        us to make a separate `get_chat_member` request for each group — on an account
        with dozens of groups, this means dozens of extra requests exactly when we are
        about to send dozens of ban requests. In the raw response, `admin_rights` is
        right there next to each chat.
    """
    if getattr(chat, "left", False) or getattr(chat, "deactivated", False):
        return None

    rights = getattr(chat, "admin_rights", None)
    is_owner = bool(getattr(chat, "creator", False))
    can_restrict = bool(rights and getattr(rights, "ban_users", False))
    if not (is_owner or can_restrict):
        return None

    if isinstance(chat, raw.types.Channel):
        # Broadcast channels do not have restrictable members in the group sense
        if not getattr(chat, "megagroup", False):
            return None
        return utils.get_channel_id(chat.id), chat.title or str(chat.id), True

    if isinstance(chat, raw.types.Chat):
        # A basic group that has migrated to a supergroup is no longer a valid target itself
        if getattr(chat, "migrated_to", None) is not None:
            return None
        return -chat.id, chat.title or str(chat.id), False

    return None


async def _scan_managed_groups(client):
    """
    Iterates through dialogs and extracts groups where we have restriction privileges.

    Pagination follows the exact pattern of pyrogram/methods/chats/get_dialogs.py, but
    applied to the raw response. A hard limit (MAX_DIALOG_PAGES) and a `seen` set are
    added to prevent infinite loops if the cursor fails to advance for any reason.
    """
    found = []
    seen = set()

    offset_date = 0
    offset_id = 0
    offset_peer = raw.types.InputPeerEmpty()

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

        for chat in getattr(r, "chats", []):
            entry = _admin_group_from_raw(chat)
            if entry is not None and entry[0] not in seen:
                seen.add(entry[0])
                found.append(entry)

        # A response of type Dialogs (rather than DialogsSlice) means this is the entire list.
        if not isinstance(r, raw.types.messages.DialogsSlice):
            break

        dialogs = [d for d in getattr(r, "dialogs", []) if isinstance(d, raw.types.Dialog)]
        if not dialogs:
            break

        # The next page's cursor is built from the last dialog: its last message date and ID,
        # plus the peer itself.
        messages = {
            utils.get_peer_id(m.peer_id): m
            for m in getattr(r, "messages", [])
            if not isinstance(m, raw.types.MessageEmpty)
        }

        last_peer_id = utils.get_peer_id(dialogs[-1].peer)
        last_message = messages.get(last_peer_id)
        if last_message is None:
            break

        offset_id = last_message.id
        offset_date = last_message.date
        offset_peer = await client.resolve_peer(last_peer_id)

    return found


async def _managed_groups(client, force: bool = False):
    """List of managed groups, with a short-lived cache."""
    global _managed_cache, _managed_cache_at

    now = time.monotonic()
    if not force and _managed_cache is not None and now - _managed_cache_at < MANAGED_CACHE_TTL:
        return _managed_cache

    _managed_cache = await _scan_managed_groups(client)
    _managed_cache_at = now
    return _managed_cache


# ==========================================================
#                    Global Operations
# ==========================================================

# Maps global actions to single-group functions. The strings are exactly what
# handlers pass, so a typo becomes a KeyError rather than a silent failure.
_GLOBAL_ACTIONS = {
    "gban": _ban,
    "ungban": _unban,
    "gmute": _mute,
    "ungmute": _unmute,
}

# Actions that are only possible in supergroups, because they route through channels.EditBanned
# and a basic group is not a channel. (gban is an exception: in a basic group, it translates
# to messages.DeleteChatUser, i.e., kicking a member — which works.)
_SUPERGROUP_ONLY_ACTIONS = ("ungban", "gmute", "ungmute")


async def _apply_everywhere(client, status, action: str, user_id: int):
    """
    Executes an action across all managed groups.

    `action` must be one of the keys in `_GLOBAL_ACTIONS`.

    Returns: (done, skipped, failed, total_groups)

    Errors for each group are handled locally: losing access to one group should not
    halt the rest of the operation. FloodWait is also handled and retried once right here
    for the affected group.
    """
    run = _GLOBAL_ACTIONS[action]
    groups = await _managed_groups(client)
    total = len(groups)
    done = skipped = failed = 0

    for index, (chat_id, title, is_supergroup) in enumerate(groups, start=1):
        if action in _SUPERGROUP_ONLY_ACTIONS and not is_supergroup:
            skipped += 1
            continue

        try:
            await run(client, chat_id, user_id)
            done += 1

        except FloodWait as e:
            # flood_seconds instead of raw e.value: its value is not guaranteed to be a number.
            delay = flood_seconds(e)
            log.warning("FloodWait %.0fs during the global action on chat %s", delay, chat_id)
            if delay > FLOOD_WAIT_LIMIT:
                failed += 1
                continue
            await asyncio.sleep(delay)
            try:
                await run(client, chat_id, user_id)
                done += 1
            except Exception as retry_error:
                failed += 1
                log.warning("global action on chat %s still failed after FloodWait: %s",
                            chat_id, type(retry_error).__name__)

        except (UserNotParticipant, UserAdminInvalid, UserCreator):
            # Not being a member, or already being an admin, is not a "failure": there was simply nothing to do.
            skipped += 1

        except Exception as e:
            failed += 1
            log.warning("global action %s failed on %r: %s",
                        action, title, type(e).__name__)

        # Progress reporting. Operations across dozens of groups take dozens of seconds.
        # Without this, the user is left staring at a frozen message.
        if status is not None and index % PROGRESS_EVERY == 0:
            try:
                await status.edit_text(
                    f"⏳ {index} از {total} گروه بررسی شد…\n"
                    f"✅ {done}   ➖ {skipped}   ❌ {failed}"
                )
            except Exception:
                # Editing the status message is just a visual detail; its failure
                # (FloodWait, MessageNotModified, etc.) must not stop the main operation.
                pass

        await asyncio.sleep(config.GLOBAL_ACTION_DELAY)

    return done, skipped, failed, total


async def _global_command(client, message, action: str, kind: str, title: str, usage: str):
    """
    Shared core logic for all four global commands.

    `kind` is the target database list (gban or gmute) and `action` is the action itself.
    The order is intentional: first register it in the list, then apply it to the groups.
    If an error occurs midway or the user stops the bot, the ID is at least in the list,
    and the auto-enforcement handler will finish the job later.
    """
    user_id, name, args, error = await _resolve_target(client, message)
    if error:
        await respond(message, error)
        return
    if user_id is None:
        await respond(message, usage)
        return

    self_error = _guard_self(client, user_id)
    if self_error:
        await respond(message, self_error)
        return

    adding = action in ("gban", "gmute")
    reason = " ".join(args).strip()

    if adding:
        await add_global_flag(kind, user_id, reason)
    else:
        was_listed = await remove_global_flag(kind, user_id)
        if not was_listed:
            log.info("user %s was not on the %s list; only lifting the restriction", user_id, kind)

    # Clear the auto-enforcement cache for this user so the new status applies
    # immediately, bypassing the anti-spam timeout.
    for key in [k for k in _ENFORCED if k[1] == user_id]:
        _ENFORCED.pop(key, None)

    status = await respond(message, "⏳ در حال اعمال روی گروه‌های تحت مدیریت…")

    done, skipped, failed, total = await _apply_everywhere(client, status, action, user_id)

    if total == 0:
        await respond(
            status or message,
            "❌ هیچ گروهی پیدا نشد که در آن اختیار محدود کردن اعضا داشته باشی.\n"
            "(فهرست گروه‌ها تا ۵ دقیقه کش می‌شود؛ اگر تازه ادمین شده‌ای کمی صبر کن.)"
        )
        return

    lines = [
        title,
        "",
        f"👤 <b>کاربر:</b> {html.escape(name)}",
        f"🆔 <code>{user_id}</code>",
        "",
        f"✅ <b>انجام شد:</b> {done} گروه",
    ]
    if skipped:
        lines.append(f"➖ <b>بدون نیاز/نشدنی:</b> {skipped}")
    if failed:
        lines.append(f"❌ <b>ناموفق:</b> {failed}")
    lines.append(f"📊 <b>کل گروه‌های تحت مدیریت:</b> {total}")
    if reason:
        lines.append(f"📝 <b>دلیل:</b> {html.escape(reason)}")

    await respond(status or message, "\n".join(lines), parse_mode=ParseMode.HTML)


@Client.on_message(AUTH_FILTER & filters.command("gban", prefixes=PREFIXES))
@safe_handler
async def gban_command(client, message):
    """Global ban: applied to all managed groups + added to the database list."""
    await _global_command(
        client, message, "gban", "gban",
        "🌐🚫 <b>مسدودسازی سراسری</b>",
        "❌ روی پیام کاربر ریپلای کن یا آیدی/یوزرنیمش را بنویس.\n"
        "مثال: `.gban @username اسپم در چند گروه`",
    )


@Client.on_message(AUTH_FILTER & filters.command(["ungban", "gunban"], prefixes=PREFIXES))
@safe_handler
async def ungban_command(client, message):
    """Lifts a global ban."""
    await _global_command(
        client, message, "ungban", "gban",
        "🌐♻️ <b>مسدودی سراسری برداشته شد</b>",
        "❌ روی پیام کاربر ریپلای کن یا آیدی/یوزرنیمش را بنویس.\n"
        "مثال: `.ungban 123456789`",
    )


@Client.on_message(AUTH_FILTER & filters.command("gmute", prefixes=PREFIXES))
@safe_handler
async def gmute_command(client, message):
    """Global mute: applied to all managed supergroups + added to the database list."""
    await _global_command(
        client, message, "gmute", "gmute",
        "🌐🔇 <b>بی‌صدای سراسری</b>",
        "❌ روی پیام کاربر ریپلای کن یا آیدی/یوزرنیمش را بنویس.\n"
        "مثال: `.gmute @username`",
    )


@Client.on_message(AUTH_FILTER & filters.command(["ungmute", "gunmute"], prefixes=PREFIXES))
@safe_handler
async def ungmute_command(client, message):
    """Lifts a global mute."""
    await _global_command(
        client, message, "ungmute", "gmute",
        "🌐🔊 <b>بی‌صدای سراسری برداشته شد</b>",
        "❌ روی پیام کاربر ریپلای کن یا آیدی/یوزرنیمش را بنویس.\n"
        "مثال: `.ungmute 123456789`",
    )


@Client.on_message(AUTH_FILTER & filters.command(["gbanlist", "gmutelist"], prefixes=PREFIXES))
@safe_handler
async def global_list_command(client, message):
    """Displays the contents of the global lists."""
    kind = "gmute" if message.command[0].lower().startswith("gmute") else "gban"
    header = "🌐🔇 <b>لیست بی‌صدای سراسری</b>" if kind == "gmute" else "🌐🚫 <b>لیست مسدودی سراسری</b>"

    entries = get_global_flags(kind)
    if not entries:
        await respond(message, f"{header}\n\nلیست خالی است.", parse_mode=ParseMode.HTML)
        return

    lines = [header, f"تعداد: {len(entries)}", ""]
    for user_id, reason in list(entries.items())[:MAX_LIST_ROWS]:
        row = f"• <code>{user_id}</code>"
        if reason:
            row += f" — {html.escape(reason)}"
        lines.append(row)

    if len(entries) > MAX_LIST_ROWS:
        lines.append(f"\n… و {len(entries) - MAX_LIST_ROWS} مورد دیگر")

    await respond(message, "\n".join(lines), parse_mode=ParseMode.HTML)


@Client.on_message(AUTH_FILTER & filters.command(["gadmin", "gstatus"], prefixes=PREFIXES))
@safe_handler
async def global_status_command(client, message):
    """Module status: managed groups count, list sizes, and auto-enforcement statistics."""
    status = await respond(message, "⏳ در حال بررسی گروه‌های تحت مدیریت…")

    # force=True because this command is executed specifically to see the *current*
    # status, and answering from a 5-minute cache defeats its purpose.
    try:
        groups = await _managed_groups(client, force=True)
    except Exception as e:
        log.warning("scanning dialogs failed: %s", type(e).__name__)
        await respond(status or message, f"❌ {_friendly_error(e)}")
        return

    supergroups = sum(1 for g in groups if g[2])
    enforce = "🟢 روشن" if config.GBAN_ENFORCE else "🔴 خاموش"

    text = (
        "🌐 **مدیریت سراسری**\n\n"
        f"گروه‌های تحت مدیریت: {len(groups)} (سوپرگروه: {supergroups})\n"
        f"لیست مسدودی: {len(GBANNED_USERS)} کاربر\n"
        f"لیست بی‌صدا: {len(GMUTED_USERS)} کاربر\n\n"
        f"اعمال خودکار: {enforce}\n"
        f"اعمال‌شده در این اجرا: {_STATS['enforced']}\n"
        f"ناموفق: {_STATS['failed']}\n\n"
        "تنظیم از طریق `GBAN_ENFORCE` در فایل `.env`"
    )
    await respond(status or message, text)


# ==========================================================
#                   Auto-Enforcement
# ==========================================================

async def _is_globally_flagged(_, __, message):
    """
    Filter: Is the sender of this message in one of the global lists?

    This function runs on *every* group message, so it only performs two in-memory dict
    lookups and never hits the disk — for the exact same reason ALLOWED_USERS is cached.
    """
    if not config.GBAN_ENFORCE:
        return False

    user = message.from_user
    if user is None:
        return False

    return user.id in GBANNED_USERS or user.id in GMUTED_USERS


globally_flagged_filter = filters.create(_is_globally_flagged)


def _recently_enforced(chat_id: int, user_id: int) -> bool:
    """
    Checks and logs the one-time enforcement status for this chat.

    Without this, if we lack admin rights in a group, every single message from a listed user
    would generate a failed API request. The logging happens *regardless of success*, since
    the goal is to prevent request spam, not to count successes.
    """
    now = time.monotonic()

    # Pruning occurs on the write path — similar to IGNORE_DELETED_IDS in db_manager.
    for key, ts in list(_ENFORCED.items()):
        if now - ts > _ENFORCED_TTL:
            _ENFORCED.pop(key, None)

    if (chat_id, user_id) in _ENFORCED:
        return True

    _ENFORCED[(chat_id, user_id)] = now
    return False


@Client.on_message(
    filters.group & filters.incoming & globally_flagged_filter,
    group=GBAN_ENFORCE_GROUP,
)
async def enforce_global_flags(client, message):
    """
    Enforces the global list on a flagged user who sent a message in a managed group.

    We deliberately do NOT use @safe_handler here: this is not a command, and on error,
    safe_handler would send a Persian error message in the group — which means spamming
    a group where we might not even be admins. Errors are solely logged.

    The dispatcher group for this handler is GBAN_ENFORCE_GROUP, not 0; the reasoning
    is explained above that constant.
    """
    user_id = message.from_user.id
    chat_id = message.chat.id

    me = getattr(client, "me", None)
    if me is not None and user_id == me.id:
        return

    if _recently_enforced(chat_id, user_id):
        return

    is_supergroup = message.chat.type == enums.ChatType.SUPERGROUP

    try:
        if user_id in GBANNED_USERS:
            await _ban(client, chat_id, user_id)
        elif is_supergroup:
            # Muting in basic groups is not possible; doing nothing is better than a repetitive error.
            await _mute(client, chat_id, user_id)
        else:
            return

        _STATS["enforced"] += 1
        log.info("enforced the global list on user %s in chat %s", user_id, chat_id)

    except (ChatAdminRequired, RightForbidden, UserAdminInvalid, UserCreator):
        # Perfectly normal state: we lack privileges in this group. The anti-spam cache
        # will prevent retries until the timeout expires.
        pass
    except FloodWait as e:
        delay = flood_seconds(e)
        log.warning("FloodWait %.0fs while enforcing the global list", delay)
        if delay <= FLOOD_WAIT_LIMIT:
            await asyncio.sleep(delay)
    except Exception as e:
        _STATS["failed"] += 1
        # The exact phrase "global-list enforcement failed" is the noise suppression key
        # in utils/logging_setup.py; if you change it here, update that regex rule too.
        log.warning("global-list enforcement failed in chat %s: %s",
                    chat_id, type(e).__name__)


# ==========================================================
#                   Visual / Joke Module
# ==========================================================

# Animation steps for .fakegban. Deliberately mimics the real .gban output.
_FAKE_STEPS = [
    "🔍 در حال جستجوی کاربر در گروه‌ها…",
    "📡 اتصال به سرورهای تلگرام…",
    "⚙️ در حال اعمال مسدودی سراسری…",
    "🧹 پاک‌سازی پیام‌های کاربر…",
    "🔒 نهایی‌سازی…",
]
FAKE_STEP_DELAY = 0.9


@Client.on_message(AUTH_FILTER & filters.command(["fakegban", "fgban"], prefixes=PREFIXES))
@safe_handler
async def fake_gban_command(client, message):
    """
    Joke command: Pretends to execute a global ban but actually does nothing.

    This function intentionally avoids calling any restriction methods and does not
    touch the database; its only API calls are to edit its own message. If anyone
    ever wants to modify this: the entire value of this module lies in its harmlessness.
    """
    user_id, name, args, error = await _resolve_target(client, message)
    if error:
        await respond(message, error)
        return
    if user_id is None:
        await respond(message, "❌ روی پیام کاربر ریپلای کن یا آیدی/یوزرنیمش را بنویس.\n"
                               "مثال: `.fakegban @username`")
        return

    status = await respond(message, _FAKE_STEPS[0])
    anchor = status or message

    for step in _FAKE_STEPS[1:]:
        await asyncio.sleep(FAKE_STEP_DELAY)
        try:
            await anchor.edit_text(step)
        except Exception:
            # If editing fails (FloodWait, message deleted, etc.), we abandon
            # the animation and jump straight to the final result.
            break

    await asyncio.sleep(FAKE_STEP_DELAY)

    # The group count is pulled from the real list to make the result believable,
    # but no requests are actually made to those groups.
    try:
        total = len(await _managed_groups(client))
    except Exception:
        total = 0
    total = total or 42

    result = (
        "🌐🚫 <b>مسدودسازی سراسری</b>\n\n"
        f"👤 <b>کاربر:</b> {html.escape(name)}\n"
        f"🆔 <code>{user_id}</code>\n\n"
        f"✅ <b>انجام شد:</b> {total} گروه\n"
        f"📊 <b>کل گروه‌های تحت مدیریت:</b> {total}"
    )
    if args:
        result += f"\n📝 <b>دلیل:</b> {html.escape(' '.join(args).strip())}"

    await respond(anchor, result, parse_mode=ParseMode.HTML)