"""
General Commands: Access control, information tools, message deletion, and forwarding.

The shared command filters (OWNER_FILTER and AUTH_FILTER) are defined here and
imported across other plugins to centralize permission management.

Conventions:
    - User-facing responses sent via `respond()` are in Persian.
    - Logs, docstrings, and inline comments are in English.
"""

import asyncio
import logging

from pyrogram import Client, enums, filters
from pyrogram.errors import (
    ChatForwardsRestricted,
    FloodWait,
    PeerIdInvalid,
    PeerIdNotSupported,
)

from utils.azan_api import get_prayer_times
from utils.currency_api import get_market_prices
from utils.db_manager import (
    ALLOWED_USERS,
    add_allowed_user,
    mark_deletions_ignored,
    remove_allowed_user,
)
from utils.gold_api import get_milli_gold_price
from utils.helpers import describe, flood_seconds, respond, safe_handler
from utils.weather_api import get_10_hours_weather

log = logging.getLogger(__name__)

PREFIXES = ["/", "."]


# --- Command Access Filters ---
async def is_allowed_func(_, __, message):
    """Check whether the incoming message originates from an authorized user ID."""
    return bool(message.from_user and message.from_user.id in ALLOWED_USERS)


dynamic_auth_filter = filters.create(is_allowed_func)

# Every command handler is built on one of these two, never on bare filters.me,
# and both refuse forwarded messages: a forward is never a command. filters.me
# also matches a message the owner forwards (from any device), and
# filters.command reads text or caption, which a forward keeps verbatim — so
# forwarding someone's ".del 10" into a chat would run it there, and an
# authorized user's forward would run with their rights. The check sits before
# filters.command, so a forward never even sets message.command. Passive
# handlers (logger, auto-seen, TTL guard, enforcement, bot cleaner) keep their
# own filters and still see forwards. ARCHITECTURE.md §8 has the limits.
OWNER_FILTER = filters.me & ~filters.forwarded
AUTH_FILTER = (filters.me | dynamic_auth_filter) & ~filters.forwarded

# --- Message Deletion Configuration ---
# Telegram accepts a maximum of 100 message IDs per deletion request.
DELETE_BATCH = 100

# Safety cap for bulk deletion to mitigate rate limits and accidental mass deletions.
MAX_DELETE_COUNT = 200

# --- Message Forwarding / Relaying Configuration ---
# Maximum number of messages allowed to be relayed in a single command execution.
MAX_SEND_COUNT = 50

# Additional scan depth to account for unforwardable entries (service/empty messages).
SEND_SCAN_HEADROOM = 25

# Delay (in seconds) between individual message copy requests during fallback mode.
COPY_DELAY = 0.4

# Maximum acceptable FloodWait sleep duration (in seconds) before aborting operations.
FLOOD_WAIT_LIMIT = 60


async def _copy_messages_one_by_one(
    client, dest_id: int, source_id: int, message_ids: list
) -> int:
    """
    Fallback mechanism when batch forwarding is blocked by protected content restrictions.

    Copies messages individually without forward headers, isolating failures to prevent
    a single problematic message (e.g., expired media) from aborting the entire batch.
    """
    sent = 0
    for msg_id in message_ids:
        try:
            await client.copy_message(dest_id, source_id, msg_id)
            sent += 1
        except FloodWait as e:
            delay = flood_seconds(e)
            if delay > FLOOD_WAIT_LIMIT:
                log.warning("FloodWait %.0fs while copying — aborting the rest", delay)
                break
            log.warning("FloodWait %.0fs while copying message %s", delay, msg_id)
            await asyncio.sleep(delay)
            try:
                await client.copy_message(dest_id, source_id, msg_id)
                sent += 1
            except Exception as retry_error:
                log.warning(
                    "Message %s could not be copied after FloodWait: %s",
                    msg_id,
                    describe(retry_error),
                )
        except Exception as e:
            log.warning("Message %s could not be copied: %s", msg_id, describe(e))

        await asyncio.sleep(COPY_DELAY)

    return sent


# --- User Access Management (Owner Only) ---
def _target_from_reply_or_arg(message):
    """
    Extract target user ID from a replied message or a numeric argument.

    Returns None if neither is provided.
    """
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user.id

    if len(message.command) == 2 and message.command[1].lstrip("-").isdigit():
        return int(message.command[1])

    return None


@Client.on_message(OWNER_FILTER & filters.command("adduser", prefixes=PREFIXES))
@safe_handler
async def add_user_command(client, message):
    """Grant authorization to a user via reply or user ID."""
    target_id = _target_from_reply_or_arg(message)
    if target_id is None:
        await respond(
            message,
            "❌ روی پیام کاربر ریپلای کن یا آیدی عددی او را وارد کن.\n"
            "مثال: `.adduser 123456`",
        )
        return

    if target_id in ALLOWED_USERS:
        await respond(message, "✅ این کاربر از قبل مجاز است.")
        return

    await add_allowed_user(target_id)
    log.info("Authorized user %s added", target_id)
    await respond(
        message,
        f"👤 کاربر `{target_id}` به لیست مجاز اضافه شد و اکنون "
        f"می‌تواند از دستورات استفاده کند.",
    )


@Client.on_message(OWNER_FILTER & filters.command("deluser", prefixes=PREFIXES))
@safe_handler
async def remove_user_command(client, message):
    """Revoke authorization from a user via reply or user ID."""
    target_id = _target_from_reply_or_arg(message)
    if target_id is None:
        await respond(
            message, "❌ روی پیام کاربر ریپلای کن یا آیدی عددی او را وارد کن."
        )
        return

    if target_id not in ALLOWED_USERS:
        await respond(message, "❌ این کاربر در لیست مجاز وجود ندارد.")
        return

    await remove_allowed_user(target_id)
    log.info("Authorized user %s removed", target_id)
    await respond(message, f"🚫 دسترسی کاربر `{target_id}` لغو شد.")


@Client.on_message(AUTH_FILTER & filters.command("ping", prefixes=PREFIXES))
@safe_handler
async def ping_command(client, message):
    """Check bot responsiveness and online status."""
    await respond(message, "یوزربات فعال و آنلاین است! 🤖✅")


# Complete command reference accessible by both owner and authorized users.
_HELP_TEXT = (
    "🤖 **دستورهای یوزربات**\n\n"
    "**هوش مصنوعی**\n"
    "`.ask <سوال>` — پرسیدن سوال (عکس/ویس/ویدیو/فایل هم می‌شود پیوست کرد)\n"
    "`.r <سوال>` — سوال درباره پیامی که رویش ریپلای کرده‌ای\n"
    "`.rp` — پیشنهاد چند جواب برای پیام طرف مقابل\n"
    "`.sum [تعداد]` — خلاصه یک پیام یا n پیام آخر چت\n"
    "`.aistop` — توقف پاسخ در حال تولید\n\n"
    "**ابزارها**\n"
    "`.tr [زبان] <متن>` — ترجمه (روی ریپلای هم کار می‌کند)\n"
    "`.ocr` — خواندن متن از عکس یا PDF\n"
    "`.stt` — تبدیل ویس/ویدیو به متن\n"
    "`.tts <متن>` — تبدیل متن به ویس (`.tts voices` برای فهرست صداها)\n"
    "`.img <توضیح>` — ساخت یا ویرایش تصویر\n\n"
    "**اطلاعات**\n"
    "`.ping` — بررسی آنلاین بودن\n"
    "`.weather <شهر>` — آب و هوا\n"
    "`.azan <شهر>` — اوقات شرعی\n"
    "`.gold` — قیمت طلا\n"
    "`.price` — قیمت لحظه‌ای ارز دیجیتال\n"
    "`.id` — آیدی چت و کاربر\n\n"
    "**مدیریت اعضا** (داخل گروه، با ریپلای یا آیدی/یوزرنیم)\n"
    "`.kick` — اخراج بدون مسدودی (می‌تواند برگردد)\n"
    "`.ban [مدت] [دلیل]` — مسدود کردن؛ مثال: `.ban 2h اسپم`\n"
    "`.unban` — رفع مسدودی\n"
    "`.mute [مدت]` / `.unmute` — بی‌صدا کردن و برگرداندن\n\n"
    "**مدیریت سراسری** (همه گروه‌های تحت مدیریت)\n"
    "`.gban [دلیل]` / `.ungban` — مسدودی سراسری\n"
    "`.gmute [دلیل]` / `.ungmute` — بی‌صدای سراسری\n"
    "`.gbanlist` / `.gmutelist` — نمایش لیست‌ها\n"
    "`.gstatus` — گروه‌های تحت مدیریت و آمار\n"
    "`.fakegban` — شوخی؛ فقط ادایش را درمی‌آورد و هیچ کاری نمی‌کند\n\n"
    "**مدیریت**\n"
    f"`.del <تعداد>` — حذف پیام‌های اخیر (حداکثر {MAX_DELETE_COUNT})\n"
    f"`.send <تعداد> <آیدی>` — انتقال پیام‌های یک پی‌وی به اینجا (حداکثر {MAX_SEND_COUNT})\n"
    "`.fwd <لینک>` — فوروارد پیام از روی لینک، حتی از کانال/گروه خصوصی (فقط خودت)\n"
    "`.botclean on|off` — حذف خودکار پیام ربات‌هایی که چتشان را پاک کرده‌ای (فقط خودت)\n"
    "`.ttl` — وضعیت نگهبان مدیای زمان‌دار\n"
    "`.adduser` / `.deluser` — مدیریت کاربران مجاز (فقط خودت)\n"
    "`.addseen` / `.delseen` — مدیریت سین خودکار (فقط خودت)\n"
    "`.ai ...` — تنظیمات هوش مصنوعی (فقط خودت)"
)


@Client.on_message(AUTH_FILTER & filters.command(["help", "cmds"], prefixes=PREFIXES))
@safe_handler
async def help_command(client, message):
    """Display the help message and command directory."""
    await respond(message, _HELP_TEXT)


@Client.on_message(AUTH_FILTER & filters.command("weather", prefixes=PREFIXES))
@safe_handler
async def weather_command(client, message):
    """Fetch 10-hour weather forecast for a specified city."""
    if len(message.command) < 2:
        await respond(message, "❌ اسم شهر را وارد کن.\nمثال: `.weather mashhad`")
        return

    # Join arguments to support multi-word city names (e.g., "New York")
    city = " ".join(message.command[1:])

    status = await respond(message, f"⏳ در حال بررسی آب و هوای {city}...")
    result = await get_10_hours_weather(city)
    await respond(status or message, result)


@Client.on_message(AUTH_FILTER & filters.command("del", prefixes=PREFIXES))
@safe_handler
async def delete_messages_command(client, message):
    """
    Delete the last N messages in the current chat.

    In groups without full message deletion privileges, only the bot owner's
    messages will be targeted.
    """
    if len(message.command) < 2 or not message.command[1].isdigit():
        await respond(message, "❌ تعداد پیام‌ها را درست وارد کن.\nمثال: `.del 10`")
        return

    count = int(message.command[1])
    if count < 1:
        await respond(message, "❌ تعداد پیام باید حداقل ۱ باشد.")
        return
    if count > MAX_DELETE_COUNT:
        await respond(
            message, f"❌ حداکثر {MAX_DELETE_COUNT} پیام در هر بار قابل حذف است."
        )
        return

    chat_id = message.chat.id

    # Verify admin privileges to delete messages from other participants
    can_delete_all = True
    if message.chat.type in (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP):
        can_delete_all = await _can_delete_others(client, chat_id)

    message_ids = []
    # Fetch chat history (+1 to include the .del command message itself)
    async for msg in client.get_chat_history(chat_id, limit=count + 1):
        if can_delete_all or (msg.from_user and msg.from_user.is_self):
            message_ids.append(msg.id)

    if not message_ids:
        await respond(message, "❌ پیامی برای حذف پیدا نشد.")
        return

    # Pre-mark message IDs in private chats to prevent anti-deletion logger alerts.
    # Must be recorded before execution to avoid race conditions with deletion updates.
    if message.chat.type == enums.ChatType.PRIVATE:
        mark_deletions_ignored(message_ids)

    deleted, failed = await _delete_in_batches(client, chat_id, message_ids)

    if failed and deleted == 0:
        await respond(
            message, "❌ حذف پیام‌ها ممکن نشد (دسترسی یا محدودیت تلگرام)."
        )
    elif failed:
        log.warning(
            "Delete: %d message(s) removed, %d failed in chat %s",
            deleted,
            failed,
            chat_id,
        )


async def _can_delete_others(client, chat_id) -> bool:
    """Check if the account possesses admin privileges to delete messages of other users."""
    me = getattr(client, "me", None)
    if me is None:
        return False

    try:
        member = await client.get_chat_member(chat_id, me.id)
    except Exception as e:
        log.info("Could not fetch membership in chat %s: %s", chat_id, describe(e))
        return False

    if getattr(member, "status", None) == enums.ChatMemberStatus.OWNER:
        return True

    privileges = getattr(member, "privileges", None)
    return bool(privileges and privileges.can_delete_messages)


async def _delete_in_batches(client, chat_id, message_ids):
    """
    Delete messages in chunks of 100 to adhere to MTProto limits.

    Returns:
        tuple[int, int]: (deleted_count, failed_count)
    """
    deleted = failed = 0

    for start in range(0, len(message_ids), DELETE_BATCH):
        batch = message_ids[start : start + DELETE_BATCH]
        try:
            await client.delete_messages(chat_id, batch)
            deleted += len(batch)
        except FloodWait as e:
            delay = flood_seconds(e)
            if delay > FLOOD_WAIT_LIMIT:
                log.warning("FloodWait %.0fs while deleting — aborting the rest", delay)
                failed += len(message_ids) - start
                break
            await asyncio.sleep(delay)
            try:
                await client.delete_messages(chat_id, batch)
                deleted += len(batch)
            except Exception as retry_error:
                failed += len(batch)
                log.warning(
                    "Delete batch failed after FloodWait: %s", describe(retry_error)
                )
        except Exception as e:
            failed += len(batch)
            log.warning("Delete batch failed in chat %s: %s", chat_id, describe(e))

    return deleted, failed


@Client.on_message(AUTH_FILTER & filters.command("gold", prefixes=PREFIXES))
@safe_handler
async def gold_price_command(client, message):
    """Fetch current gold and coin prices."""
    status = await respond(message, "⏳ در حال دریافت قیمت طلا...")
    result = await get_milli_gold_price()
    await respond(status or message, result)


@Client.on_message(AUTH_FILTER & filters.command("azan", prefixes=PREFIXES))
@safe_handler
async def azan_command(client, message):
    """Fetch Islamic prayer times for a specified city."""
    if len(message.command) < 2:
        await respond(
            message, "❌ اسم شهر را وارد کن.\nمثال: `.azan Mashhad` یا `.azan تهران`"
        )
        return

    city = " ".join(message.command[1:])
    status = await respond(message, f"⏳ در حال دریافت اوقات شرعی {city}...")
    result = await get_prayer_times(city)
    await respond(status or message, result)


@Client.on_message(AUTH_FILTER & filters.command("id", prefixes=PREFIXES))
@safe_handler
async def get_id_command(client, message):
    """Retrieve chat ID, chat type, and replied user ID if applicable."""
    chat_id = message.chat.id
    chat_type = message.chat.type.name if message.chat.type else "نامشخص"

    reply_info = ""
    if message.reply_to_message and message.reply_to_message.from_user:
        reply_id = message.reply_to_message.from_user.id
        reply_info = f"\n👤 آیدی کاربر ریپلای شده: `{reply_id}`"

    await respond(
        message,
        f"📍 **اطلاعات این مکان ({chat_type}):**\n\n"
        f"🆔 آیدی چت: `{chat_id}`{reply_info}",
    )


@Client.on_message(
    AUTH_FILTER & filters.command(["price", "market"], prefixes=PREFIXES)
)
@safe_handler
async def market_prices_command(client, message):
    """Display real-time market prices for USDT, BTC, and ETH."""
    requester = message.from_user.id if message.from_user else "anonymous"
    log.info("Market prices requested by %s", requester)

    status = await respond(message, "⏳ در حال دریافت لحظه‌ای قیمت‌ها...")
    result = await get_market_prices()
    await respond(status or message, result)


@Client.on_message(AUTH_FILTER & filters.command(["send"], prefixes=PREFIXES))
@safe_handler
async def send_messages_command(client, message):
    """
    Relay the last N messages from a target private chat into the current chat.

    Syntax: `.send <count> <target_user_id>`
    """
    usage = (
        "❌ سینتکس درست: `.send <تعداد> <آیدی کاربر>`\n"
        "مثال: `.send 10 123456789`\n"
        f"حداکثر تعداد مجاز: {MAX_SEND_COUNT}"
    )

    # 1. Argument Validation
    if len(message.command) != 3:
        await respond(message, usage)
        return

    try:
        count = int(message.command[1])
        target_id = int(message.command[2])
    except ValueError:
        await respond(message, usage)
        return

    if count < 1:
        await respond(message, "❌ تعداد پیام باید حداقل ۱ باشد.")
        return

    if count > MAX_SEND_COUNT:
        await respond(
            message, f"❌ حداکثر {MAX_SEND_COUNT} پیام در هر بار مجاز است."
        )
        return

    requester = message.from_user.id if message.from_user else "anonymous"
    log.info(
        "Send: relaying %s message(s) from %s, requested by %s",
        count,
        target_id,
        requester,
    )

    status = await respond(
        message, f"⏳ در حال خواندن {count} پیام آخر از پی‌وی `{target_id}`..."
    )

    # 2. Peer Resolution & Verification
    try:
        target_chat = await client.get_chat(target_id)
    except (PeerIdInvalid, PeerIdNotSupported, ValueError, KeyError) as e:
        log.info("Send: peer %s is not resolvable: %s", target_id, type(e).__name__)
        await respond(
            status or message,
            f"❌ کاربر `{target_id}` پیدا نشد.\n"
            "یوزربات باید قبلا با این اکانت گفتگو کرده باشد تا آیدی‌اش در سشن ذخیره شود.",
        )
        return

    if target_chat.type not in (enums.ChatType.PRIVATE, enums.ChatType.BOT):
        await respond(
            status or message,
            f"❌ `{target_id}` یک پی‌وی نیست (نوع: {target_chat.type.name}).\n"
            "این دستور فقط برای گفتگوی خصوصی با یک کاربر کار می‌کند.",
        )
        return

    # 3. Message Collection & Filtering
    skip_ids = set()
    if message.chat.id == target_chat.id:
        skip_ids.add(message.id)
        if status is not None:
            skip_ids.add(status.id)

    message_ids = []
    async for msg in client.get_chat_history(
        target_chat.id, limit=count + SEND_SCAN_HEADROOM
    ):
        if msg.service or msg.empty:
            continue
        if msg.id in skip_ids:
            continue

        message_ids.append(msg.id)
        if len(message_ids) >= count:
            break

    if not message_ids:
        await respond(status or message, "❌ پیام قابل انتقالی در این پی‌وی پیدا نشد.")
        return

    # Reverse list to preserve chronological conversation order (oldest to newest)
    message_ids.reverse()

    # 4. Message Relaying
    dest_id = message.chat.id
    try:
        await client.forward_messages(dest_id, target_chat.id, message_ids)
        relayed = len(message_ids)
    except ChatForwardsRestricted:
        log.warning(
            "Send: forwarding is restricted in chat %s; falling back to per-message copy",
            target_chat.id,
        )
        relayed = await _copy_messages_one_by_one(
            client, dest_id, target_chat.id, message_ids
        )
        if relayed == 0:
            await respond(
                status or message,
                "❌ این چت محتوای محافظت‌شده دارد و انتقال پیام‌ها امکان‌پذیر نبود.",
            )
            return

    # 5. Output Summary
    title = target_chat.first_name or target_chat.username or str(target_id)
    summary = f"✅ {relayed} پیام از «{title}» منتقل شد."
    if relayed < count:
        summary += (
            f"\n(از {count} پیام درخواستی؛ تاریخچه کمتر بود یا پیام‌های غیرقابل انتقال رد شدند)"
        )

    # Disable markdown parsing to avoid formatting errors caused by unescaped user display names
    await respond(status or message, summary, parse_mode=enums.ParseMode.DISABLED)