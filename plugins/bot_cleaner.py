"""
Bot-chat cleaner — delete what a bot sends into a chat you already deleted.

A bot you once started keeps the right to message you after you delete the
chat, and the only built-in way to stop it is blocking it — which fills the
block list with advertising bots. With `.botclean on`, a bot message that
lands in an otherwise empty chat is deleted together with the chat entry,
exactly as "Delete chat" would do it, and the bot stays unblocked.

What counts as "a chat you deleted":
    The chat holds nothing but this bot's own recent messages: nothing you
    sent, no service message, and nothing older than BURST_WINDOW. A bot can
    only reach someone who wrote to it or granted it access (a login widget, a
    mini app), and both leave a trace — your message, or a service line. A
    chat with neither has been emptied by hand.

    BURST_WINDOW exists because updates are not handled strictly in order: two
    ads sent a second apart can reach this module newest-first, and the second
    must not mistake the first for older history and spare the bot. Ten
    minutes also covers a short reconnect. A bot message older than that which
    is still in the chat (it arrived while the userbot was down, or before the
    switch was on) counts as history, so such a chat is left alone until it is
    deleted once by hand. That is the safe direction to be wrong in; the other
    direction deletes messages.

Why only "keep" is cached:
    Most bot messages come from bots in active use, and those are the ones
    worth not checking every time, so a "keep" verdict is remembered for
    KEEP_TTL. A "delete" verdict is never cached: the history read behind it
    is also what proves, at that moment, that nothing of yours is in the chat
    — it is what makes the deletion safe. Ads are rare enough that one read
    per ad costs nothing.

    A cached "keep" goes stale the moment the chat is deleted on another
    device. Telegram's deletion update names no chat for private messages, so
    the ids of every message seen while a chat was in use are remembered, and
    a deletion that touches any of them drops that bot's verdict. The next
    read then decides — and a remembered message that is still there proves
    the chat was not cleared: a bot deleting your /start, or you deleting one
    message, leaves the rest of the conversation behind.

Scope: private chats with bots. Your own messages there only mark the bot as
in use; Telegram's service accounts are never touched.
"""

import asyncio
import html
import logging
import time
from collections import OrderedDict, deque

from pyrogram import Client, enums, filters, raw
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait

from utils.db_manager import get_setting, mark_deletions_ignored, set_setting
from utils.helpers import describe, flood_seconds, respond, safe_handler, time_text

log = logging.getLogger(__name__)

PREFIXES = ["/", "."]

# --- Dispatcher group ---
# Same constraint as logger (2) and auto_seen (1): the dispatcher runs at most
# one handler per group, and the watcher below matches every message in a bot
# chat, so in group 0 it would swallow any command typed there. Its own group,
# after the others, also means every other handler has seen a message before
# this one can delete it.
BOT_CLEANER_GROUP = 3

# Key in the settings table; the stored value is "on" or "off".
SETTING_KEY = "bot_cleaner"

# How many messages one history read asks for. A burst into an emptied chat is
# a handful of messages; a chat that returns more than this was not emptied.
PROBE_LIMIT = 30

# Seconds between the newest and the oldest message of what still counts as
# one burst (see the module docstring).
BURST_WINDOW = 10 * 60

# How long a "this bot is in use" verdict is trusted without a fresh read.
KEEP_TTL = 6 * 3600

# Message ids remembered from chats in use, so a deletion made on another
# device can be traced back to its chat. A leak guard, not a tuning knob: an
# id that falls off the end is old enough for BURST_WINDOW to protect anyway.
TRACKED_IDS_MAX = 5000

# How long the ids of a burst we deleted are remembered. Handlers for the rest
# of that burst may still be queued behind the lock; they must not re-read a
# history that is already gone.
DELETED_TTL = 600

# messages.deleteHistory answers with an offset while work remains, and the
# documented contract is to repeat the call until it reaches zero. A burst is
# far below the size that needs that, so this only bounds the loop.
DELETE_ROUNDS = 5

# Telegram's own accounts. Several are flagged as bots, and each one carries
# something that must never vanish just because its chat was once cleared.
NEVER_CLEAN_IDS = frozenset({
    777000,       # Telegram — login codes, security notices (not a bot; belt and braces)
    93372553,     # @BotFather — bot-token and security notices
    489000,       # Verification Codes — codes sent through the Telegram Gateway
    1271266957,   # @replies — answers to your comments under channel posts
    1087968824,   # @GroupAnonymousBot
    136817688,    # @Channel_Bot
})

# --- Runtime state (RAM only; rebuilt from scratch after a restart) ---
_KEEP = {}                # {bot_id: monotonic expiry of its "keep" verdict}
_OWNER = OrderedDict()    # {message_id: bot_id} seen while the chat was in use, oldest first
_DELETED = {}             # {message_id: monotonic time we deleted it}
_LOCKS = {}               # {bot_id: asyncio.Lock}

# Counters for `.botclean`, per run.
_STATS = {"messages": 0, "chats": 0, "failed": 0}

# The last few clean-ups, shown by `.botclean`. A false positive has to be
# visible somewhere a user actually looks, not only in a log.
_LAST_EVENTS = deque(maxlen=8)


def _enabled() -> bool:
    return get_setting(SETTING_KEY) == "on"


def _reset_state() -> None:
    """Forget every verdict, so a switch flip starts from what the chats hold now."""
    _KEEP.clear()
    _OWNER.clear()
    _DELETED.clear()


def _track(message_id: int, bot_id: int) -> None:
    """
    Remember a message of a chat in use, and which bot chat it belongs to.

    Only messages from a chat judged to be in use come here — never one that
    is still waiting for its verdict — because _burst_ids() reads a surviving
    tracked message as proof that the chat was not cleared.
    """
    _OWNER[message_id] = bot_id
    _OWNER.move_to_end(message_id)
    while len(_OWNER) > TRACKED_IDS_MAX:
        _OWNER.popitem(last=False)


def _mark_keep(bot_id: int) -> None:
    _KEEP[bot_id] = time.monotonic() + KEEP_TTL


def _keep_valid(bot_id: int) -> bool:
    expiry = _KEEP.get(bot_id)
    if expiry is None:
        return False
    if time.monotonic() >= expiry:
        _KEEP.pop(bot_id, None)
        return False
    return True


def _remember_deleted(ids) -> None:
    """Record a burst we deleted, pruning old entries on write."""
    now = time.monotonic()
    for message_id, at in list(_DELETED.items()):
        if now - at > DELETED_TTL:
            _DELETED.pop(message_id, None)
    for message_id in ids:
        _DELETED[message_id] = now


def _lock_for(bot_id: int) -> asyncio.Lock:
    """
    One lock per bot, so a burst is judged once instead of once per message.

    Idle locks are dropped when the table grows, as a leak guard. Dropping one
    at the wrong instant costs at most a second history read for that bot.
    """
    lock = _LOCKS.get(bot_id)
    if lock is None:
        if len(_LOCKS) > 200:
            for key in [key for key, held in _LOCKS.items() if not held.locked()]:
                _LOCKS.pop(key, None)
        lock = _LOCKS[bot_id] = asyncio.Lock()
    return lock


def _bot_label(chat) -> str:
    """How a bot is named in the status: @username, else its name, else its id."""
    username = getattr(chat, "username", None)
    if username:
        return f"@{username}"
    name = (getattr(chat, "first_name", "") or "").strip()
    return name or str(getattr(chat, "id", "?"))


def _burst_ids(history, bot_id: int):
    """
    Decide from one raw history read.

    Returns the ids to delete when the chat is nothing but a fresh burst from
    the bot, an empty list when the chat is already empty, and None when the
    chat has to be left alone.

    The raw response is used rather than parsed messages because parsing can
    drop what it fails to understand, and a message this function never saw
    must not end up below the max_id the deletion runs up to.
    """
    messages = [
        m for m in getattr(history, "messages", None) or []
        if not isinstance(m, raw.types.MessageEmpty)
    ]

    # A slice that announces more messages than it carries: there is history
    # beyond this read, and a chat with that much in it was not deleted.
    total = getattr(history, "count", None)
    if total is not None and total > len(messages):
        return None
    if len(messages) >= PROBE_LIMIT:
        return None

    if not messages:
        return []

    newest = max(m.date for m in messages)

    for m in messages:
        # A service line means access was granted (login widget, mini app), a
        # payment was made, something was pinned — a chat in use.
        if not isinstance(m, raw.types.Message):
            return None
        # Anything you sent, from any device.
        if m.out:
            return None
        # Seen while the chat was in use and still here: whatever was deleted
        # since, the chat itself was not.
        if _OWNER.get(m.id) == bot_id:
            return None
        # Older than this burst: the chat was not empty when it began.
        if newest - m.date > BURST_WINDOW:
            return None

    return [m.id for m in messages]


async def _evaluate(client, chat) -> None:
    """Read the chat once and either remember it as in use or delete the burst."""
    bot_id = chat.id
    peer = await client.resolve_peer(bot_id)

    history = await client.invoke(
        raw.functions.messages.GetHistory(
            peer=peer,
            offset_id=0,
            offset_date=0,
            add_offset=0,
            limit=PROBE_LIMIT,
            max_id=0,
            min_id=0,
            hash=0,
        )
    )

    ids = _burst_ids(history, bot_id)

    if ids is None:
        _mark_keep(bot_id)
        # Every id seen here is one more chance to notice the chat being
        # deleted on another device later.
        for m in getattr(history, "messages", None) or []:
            message_id = getattr(m, "id", None)
            if message_id:
                _track(message_id, bot_id)
        log.debug("bot-clean: chat with bot %s is in use; leaving it", bot_id)
        return

    if not ids:
        # Already empty: the burst was deleted by someone else in the meantime.
        return

    await _delete_burst(client, peer, chat, ids)


async def _delete_burst(client, peer, chat, ids) -> None:
    """
    Delete the chat up to the newest message of the burst — "Delete chat".

    messages.deleteHistory with just_clear unset is what the official apps
    send for "Delete chat": the messages go and so does the entry in the chat
    list. Deleting the messages one by one would leave an empty chat sitting in
    the list, which is the clutter this module exists to remove. It is safe
    only because the read just before showed the whole chat: every message up
    to max_id is one of `ids`, and anything newer has a higher id and is left
    for its own handler.

    revoke stays unset, as in the official apps for a bot: the bot never sees
    a deletion in a private chat anyway.
    """
    bot_id = chat.id

    # Registered before the call, like .del: the deletion update can arrive the
    # instant it returns. Bot chats are never cached by the logger, so this is
    # belt and braces rather than a fix.
    mark_deletions_ignored(ids)
    _remember_deleted(ids)

    for _ in range(DELETE_ROUNDS):
        result = await client.invoke(
            raw.functions.messages.DeleteHistory(
                peer=peer,
                max_id=max(ids),
                just_clear=False,
                revoke=False,
            )
        )
        if not getattr(result, "offset", 0):
            break

    _STATS["messages"] += len(ids)
    _STATS["chats"] += 1
    _LAST_EVENTS.appendleft((time_text(fmt="%H:%M:%S"), _bot_label(chat), len(ids)))
    log.info("bot-clean: deleted %d message(s) from bot %s (%s)",
             len(ids), bot_id, _bot_label(chat))


async def _handle(client, message) -> None:
    chat = message.chat
    bot_id = chat.id

    if bot_id in NEVER_CLEAN_IDS:
        return

    # Anything you send there — a /start, a command, a reply from your phone —
    # means the chat is in use; so does a service line (access granted through
    # a login widget or a mini app, a payment). No read needed.
    if message.outgoing or message.service:
        _track(message.id, bot_id)
        _mark_keep(bot_id)
        return

    if _keep_valid(bot_id):
        _track(message.id, bot_id)
        return

    async with _lock_for(bot_id):
        # Re-checked under the lock: whoever held it may have just decided this
        # very burst, one way or the other.
        if _keep_valid(bot_id):
            _track(message.id, bot_id)
            return
        if message.id in _DELETED:
            return
        # Not tracked before the verdict: a keep tracks every id it read, and
        # a delete leaves nothing to track.
        await _evaluate(client, chat)


async def _in_bot_chat(_, __, message) -> bool:
    chat = getattr(message, "chat", None)
    return chat is not None and chat.type == enums.ChatType.BOT


in_bot_chat = filters.create(_in_bot_chat)


@Client.on_message(in_bot_chat, group=BOT_CLEANER_GROUP)
async def watch_bot_chats(client, message):
    # No @safe_handler: this runs on every message in a bot chat, and its error
    # reply would land in that chat. Failures are logged only, under a prefix
    # utils/logging_setup.py throttles — keep the two in sync.
    if not _enabled():
        return

    try:
        await _handle(client, message)
    except FloodWait as e:
        _STATS["failed"] += 1
        log.warning("bot-clean failed in chat %s: FloodWait %.0fs; message left alone",
                    message.chat.id, flood_seconds(e))
    except Exception as e:
        _STATS["failed"] += 1
        log.warning("bot-clean failed in chat %s: %s", message.chat.id, describe(e))


@Client.on_deleted_messages(group=BOT_CLEANER_GROUP)
async def forget_deleted(client, messages):
    """
    Drop a cached "keep" when the chat it was based on loses messages.

    Deleting a chat on the phone reaches us as a bare list of ids — a private
    deletion never names its chat — so the ids remembered in _track() are the
    only way to tell which bot it was. The next message from that bot is then
    judged afresh. A bot deleting one of its own messages lands here too and
    merely costs one extra read.
    """
    if not _enabled():
        return

    try:
        for msg in messages:
            # Channel and supergroup deletions carry their chat; bot chats never do.
            if getattr(msg, "chat", None) is not None:
                continue
            bot_id = _OWNER.pop(msg.id, None)
            if bot_id is not None:
                _KEEP.pop(bot_id, None)
    except Exception as e:
        log.warning("bot-clean failed while reading a deletion: %s", describe(e))


# --- The switch ---

_USAGE = (
    "❌ استفاده:\n"
    "`.botclean on` — روشن کردن\n"
    "`.botclean off` — خاموش کردن\n"
    "`.botclean` — وضعیت"
)

_ON_TEXT = (
    "🧹 پاک‌کن چت ربات‌ها روشن شد.\n\n"
    "از این به بعد اگر رباتی در چتی پیام بدهد که آن را پاک کرده‌ای، پیامش همراه "
    "با خود چت حذف می‌شود؛ بدون بلاک کردن ربات.\n\n"
    "• چتی که پیامی از خودت در آن باشد دست نمی‌خورد؛ ربات‌هایی که با آن‌ها کار "
    "می‌کنی امن‌اند.\n"
    "• اگر پیام‌های رباتی را می‌خواهی، کافی است یک پیام (مثلاً /start) به آن بدهی.\n"
    "• چتی که همین حالا پیام ربات در آن مانده، یک بار دستی پاکش کن؛ از دفعه بعد "
    "خودکار پاک می‌شود."
)

_OFF_TEXT = "⏸ پاک‌کن چت ربات‌ها خاموش شد؛ پیام ربات‌ها دیگر حذف نمی‌شود."


def _status_text() -> str:
    """The status block. HTML, because bot names are arbitrary text."""
    state = "🟢 روشن" if _enabled() else "🔴 خاموش"
    in_use = sum(1 for bot_id in list(_KEEP) if _keep_valid(bot_id))

    lines = [
        "🧹 <b>پاک‌کن چت ربات‌ها</b>",
        "",
        f"وضعیت: {state}",
        f"پاک‌شده در این اجرا: {_STATS['messages']} پیام از {_STATS['chats']} چت",
        f"ربات‌های در حال استفاده (در حافظه): {in_use}",
    ]
    if _STATS["failed"]:
        lines.append(f"خطا: {_STATS['failed']} (جزئیات در لاگ)")

    if _LAST_EVENTS:
        lines += ["", "<b>آخرین پاک‌سازی‌ها:</b>"]
        for stamp, label, count in _LAST_EVENTS:
            lines.append(f"<code>{stamp}</code> · {html.escape(label)} · {count} پیام")

    lines += ["", "<code>.botclean on</code> / <code>.botclean off</code>"]
    return "\n".join(lines)


@Client.on_message(filters.me & filters.command("botclean", prefixes=PREFIXES))
@safe_handler
async def bot_clean_command(client, message):
    """Turn the cleaner on or off, or show its state."""
    arg = message.command[1].lower() if len(message.command) > 1 else "status"

    if arg == "on":
        if not _enabled():
            await set_setting(SETTING_KEY, "on")
            _reset_state()
            log.info("bot-chat cleaner turned on")
        await respond(message, _ON_TEXT)
        return

    if arg == "off":
        if _enabled():
            await set_setting(SETTING_KEY, "off")
            _reset_state()
            log.info("bot-chat cleaner turned off")
        await respond(message, _OFF_TEXT)
        return

    if arg == "status":
        await respond(message, _status_text(), parse_mode=ParseMode.HTML)
        return

    await respond(message, _USAGE)
