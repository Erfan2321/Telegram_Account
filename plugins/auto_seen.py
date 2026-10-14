"""
Auto-read module: automatically marks incoming messages as read for specified chats.

Note:
    The main handler intentionally avoids using @safe_handler to prevent sending
    error responses into the chat on every incoming event. Failures are logged,
    and log rate-limiting relies on the exact log prefix configured in utils/logging_setup.py.
"""

import html
import logging

from pyrogram import Client, filters
from pyrogram.enums import ParseMode

from plugins.general import OWNER_FILTER
from utils.db_manager import AUTO_SEEN_CHATS, add_auto_seen, remove_auto_seen
from utils.helpers import describe, respond, safe_handler

log = logging.getLogger(__name__)

PREFIXES = ["/", "."]


# Dynamic filter to check chat ID against the in-memory cache
async def is_auto_seen_func(_, __, message):
    return message.chat.id in AUTO_SEEN_CHATS


dynamic_seen_filter = filters.create(is_auto_seen_func)


# --- Dispatcher Group ---
# Same reason as plugins/logger.py: the dispatcher breaks out of a group after the
# first handler that runs, so an always-matching handler in group 0 hides every
# command registered after it. Here it matched any incoming message in a chat added
# with `.addseen`, which silently killed authorized users' commands in exactly those
# chats.
#
# The group must stay *greater* than -1: ttl_guard's raw handler lives in -1 and has
# to download self-destructing media before `read_chat_history` marks it read (§8.2).
# A positive group keeps that ordering and puts the read receipt after command dispatch.
AUTO_SEEN_GROUP = 1


# 1. Main auto-read message handler
@Client.on_message(dynamic_seen_filter & filters.incoming, group=AUTO_SEEN_GROUP)
async def auto_read_handler(client, message):
    # Intentionally omitting @safe_handler: this runs on every incoming message,
    # so error messages must not be dispatched to the chat. Failures are logged only.
    try:
        await client.read_chat_history(chat_id=message.chat.id)
    except Exception as e:
        # The prefix "auto-seen failed" is matched in utils/logging_setup.py for log throttling
        log.warning("auto-seen failed for chat %s: %s", message.chat.id, describe(e))


async def _resolve_target(client, message):
    """
    Resolve the target chat ID and title for addseen/delseen commands.

    Resolution order:
        1. Forwarded message source (explicit reply)
        2. Command argument (username or ID)
        3. Current chat context

    Returns:
        tuple: (chat_id, chat_title, error_message).
               error_message is None on success.
    """
    reply = message.reply_to_message
    if reply is not None and reply.forward_from_chat is not None:
        source = reply.forward_from_chat
        return source.id, source.title or str(source.id), None

    if len(message.command) > 1:
        target = message.command[1]
        try:
            # Resolves username or accessible chat ID to a chat entity
            chat = await client.get_chat(target)
        except Exception as e:
            log.info("auto-seen: could not resolve %r: %s", target, describe(e))
            return None, "", "❌ یوزرنیم یا آیدی نامعتبر است یا اکانت در آن چت عضو نیست."

        return chat.id, chat.title or chat.first_name or str(chat.id), None

    chat = message.chat
    return chat.id, chat.title or chat.first_name or str(chat.id), None


# 2. Command: Add chat to auto-read list
@Client.on_message(OWNER_FILTER & filters.command("addseen", prefixes=PREFIXES))
@safe_handler
async def add_seen_cmd(client, message):
    target_chat_id, chat_title, error = await _resolve_target(client, message)
    if error:
        await respond(message, error)
        return

    if target_chat_id in AUTO_SEEN_CHATS:
        await respond(message, f"✅ چت «{html.escape(chat_title)}» از قبل در لیست سین خودکار است.",
                      parse_mode=ParseMode.HTML)
        return

    await add_auto_seen(target_chat_id)
    log.info("auto-seen list: added chat %s", target_chat_id)
    # Chat title is untrusted input; escape HTML entities to prevent formatting injection
    await respond(message,
                  f"👀 چت «{html.escape(chat_title)}» (<code>{target_chat_id}</code>) "
                  f"به لیست سین خودکار اضافه شد.",
                  parse_mode=ParseMode.HTML)


# 3. Command: Remove chat from auto-read list
@Client.on_message(OWNER_FILTER & filters.command("delseen", prefixes=PREFIXES))
@safe_handler
async def remove_seen_cmd(client, message):
    target_chat_id, chat_title, error = await _resolve_target(client, message)
    if error:
        await respond(message, error)
        return

    if target_chat_id not in AUTO_SEEN_CHATS:
        await respond(message, "❌ این چت در لیست سین خودکار نیست.")
        return

    await remove_auto_seen(target_chat_id)
    log.info("auto-seen list: removed chat %s", target_chat_id)
    await respond(message,
                  f"🚫 چت «{html.escape(chat_title)}» (<code>{target_chat_id}</code>) "
                  f"از لیست سین خودکار حذف شد.",
                  parse_mode=ParseMode.HTML)