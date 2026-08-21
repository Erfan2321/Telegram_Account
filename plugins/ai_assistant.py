"""
AI Assistant Plugin for Telegram Userbot.

This module acts as the presentation and interaction layer: it handles Telegram
commands, manages conversational memory/history, and streams LLM generation tokens
directly into editable Telegram messages in real time. Backend networking and model
invocations are managed in `utils/ai_engines.py`.

Commands:
    .ask     Ask a question (supports photo, voice, audio, video, and document attachments)
    .r       Ask a contextual question about a replied message
    .rp      Generate reply suggestions for a replied message
    .sum     Summarize a single message or the last N messages in the chat
    .aistop  Halt ongoing streaming generation for the current chat
    .ai      Configuration and settings (owner-only)
"""

import asyncio
import logging
import time
from contextlib import aclosing

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait, MessageNotModified

import config
from plugins.ai_tools import friendly_error
from plugins.general import AUTH_FILTER
from utils.ai_context import (
    Collected,
    chat_history_text,
    collect,
    quote_block,
    sender_name,
    strip_command,
)
from utils.ai_engines import MODELS, AIError, list_models, resolve_model, stream_chat
from utils.db_manager import (
    add_ai_message,
    clear_ai_history,
    count_ai_history,
    get_ai_history,
    get_ai_settings,
    set_ai_model,
    set_ai_persona,
)
from utils.helpers import describe, flood_seconds, respond, safe_handler

log = logging.getLogger(__name__)

PREFIXES = ["/", "."]

# Safe character threshold before splitting a message (Telegram hard limit is 4096 characters).
SAFE_LIMIT = 3900
# Minimum cooldown interval between message edit requests during streaming (seconds).
EDIT_INTERVAL = 1.2
# Maximum character length per stored message in conversational memory to prevent token bloating.
MEMORY_ITEM_LIMIT = 4000

# Chat history limits for the .sum summarization command.
SUM_MAX_MESSAGES = 300
SUM_DEFAULT_MESSAGES = 50

# Maximum FloodWait sleep threshold during streaming edits before dropping the edit attempt.
FLOOD_WAIT_LIMIT = 60

# Active streaming cancellation triggers per chat: {chat_id: asyncio.Event}
_stop_flags: dict[int, asyncio.Event] = {}

BASE_SYSTEM_PROMPT = (
    "You are a helpful assistant embedded in a Telegram userbot.\n"
    "LANGUAGE: Always reply in the SAME language the user wrote in. "
    "If they write in Persian, answer in Persian. If English, answer in English. "
    "Keep code, identifiers, commands and technical terms in English regardless.\n"
    "STYLE: Be concise — this is a chat app, not a document. Prefer short paragraphs "
    "and bullet points over long prose. Get to the point immediately.\n"
    "FORMATTING: Use only Telegram-compatible Markdown: **bold**, __italic__, "
    "`inline code`, and ``` fenced code blocks. "
    "Never use headings (#, ##) or Markdown tables — Telegram does not render them. "
    "Use a bulleted list instead of a table.\n"
    "ATTACHMENTS: The user may attach images, voice messages, audio, video or files. "
    "Answer about the attached content directly; never claim you cannot see or hear it."
)


def _balance_fences(text: str) -> str:
    """
    Ensure unclosed markdown code blocks are properly terminated.

    Prevents Markdown parsing failures on Telegram when streamed text cuts off
    inside an open code fence.
    """
    if text.count("```") % 2 == 1:
        return text + "\n```"
    return text


class StreamEditor:
    """
    Incrementally updates a Telegram message with streaming LLM tokens.

    Key mechanisms:
    - Edits are throttled to respect Telegram MTProto rate limits.
    - Markdown parsing is disabled during streaming to avoid syntax errors on partial tokens,
      and applied only during the final render.
    - Dynamically adapts edit intervals upon encountering FloodWait errors.
    """

    def __init__(self, anchor, header: str = "", min_interval: float = EDIT_INTERVAL):
        self.anchor = anchor
        self.header = header
        self.min_interval = min_interval
        self.buffer = ""          # Current chunk text
        self.full_text = ""       # Complete generated text across all chunks (for memory persistence)
        self.last_edit = 0.0
        self.last_rendered = None

    async def feed(self, piece: str):
        """Append a stream token chunk and trigger a throttled message update."""
        self.buffer += piece
        self.full_text += piece

        # Rollover to a new message if current buffer reaches safe capacity
        if len(self.buffer) >= SAFE_LIMIT:
            await self._rollover()
            return

        await self._throttled_edit()

    async def _rollover(self):
        """Finalize the current message and spawn a new continuation message."""
        cut = self.buffer.rfind("\n", 0, SAFE_LIMIT)
        if cut <= 0:
            cut = SAFE_LIMIT
        head, tail = self.buffer[:cut], self.buffer[cut:].lstrip("\n")

        await self._render(_balance_fences(head), ParseMode.MARKDOWN)

        try:
            self.anchor = await self.anchor.reply_text(
                "…", parse_mode=ParseMode.DISABLED, quote=False
            )
        except Exception:
            log.exception("Could not open a continuation message for the answer")
            raise

        # Clear header so user prompt is only displayed in the primary message
        self.header = ""

        # Preserve unclosed code fence into continuation buffer
        if head.count("```") % 2 == 1:
            tail = "```\n" + tail

        self.buffer = tail
        self.last_rendered = None
        self.last_edit = 0.0

    async def _throttled_edit(self):
        """Dispatch edit request if the elapsed time exceeds the minimum edit interval."""
        now = time.monotonic()
        if now - self.last_edit < self.min_interval:
            return
        await self._render(self.buffer + " ▌", ParseMode.DISABLED)

    async def _render(self, text: str, parse_mode):
        """
        Execute an edit request against the anchor message with comprehensive error handling.

        Dispatches exactly one edit request per invocation to avoid rate limit spikes.
        """
        final_text = (self.header + text).strip()

        if not final_text or final_text == self.last_rendered:
            return

        try:
            await self.anchor.edit_text(final_text, parse_mode=parse_mode)
            self.last_rendered = final_text
            self.last_edit = time.monotonic()

        except MessageNotModified:
            self.last_rendered = final_text
            self.last_edit = time.monotonic()

        except FloodWait as e:
            # Dynamically increase throttle interval on FloodWait
            delay = flood_seconds(e)
            self.min_interval = max(self.min_interval, delay + 0.5)
            log.warning(
                "FloodWait %.0fs while streaming; edit interval is now %.1fs",
                delay,
                self.min_interval,
            )
            if delay <= FLOOD_WAIT_LIMIT:
                await asyncio.sleep(delay)
            self.last_edit = time.monotonic()

        except Exception as e:
            # Fall back to plain text if Markdown parsing fails
            if parse_mode is not ParseMode.DISABLED:
                log.warning("Markdown render failed (%s); retrying as plain text", describe(e))
                try:
                    await self.anchor.edit_text(final_text, parse_mode=ParseMode.DISABLED)
                    self.last_rendered = final_text
                    self.last_edit = time.monotonic()
                except Exception:
                    log.exception("Editing message failed even as plain text")
            else:
                log.warning("Editing message failed: %s", describe(e))

    async def finish(self, footer: str = ""):
        """Apply final edit: balance code fences and apply Markdown formatting."""
        text = _balance_fences(self.buffer.strip()) + footer
        if not text.strip():
            text = "⚠️ پاسخی از سرویس دریافت نشد."
        self.last_rendered = None  # Force final rendering
        await self._render(text, ParseMode.MARKDOWN)

    async def show(self, text: str):
        """Render a standalone static text (e.g., error notification) on the anchor message."""
        self.last_rendered = None
        await self._render(text, ParseMode.DISABLED)


# ==========================================================
#                   Shared Command Helpers
# ==========================================================

def _extract_flags(raw: str) -> tuple[str | None, bool, str]:
    """
    Extract leading configuration flags (model aliases, extended mode) from the prompt.

    Example: `.ask pro extended Why is the sky blue?`
    Consumes leading recognized model aliases (from `MODELS`) and the 'extended' keyword.
    Returns the remaining text untouched as the prompt.
    """
    model = None
    extended = False

    while raw:
        pieces = raw.split(None, 1)
        head = pieces[0].lower()
        rest = pieces[1] if len(pieces) > 1 else ""

        if head in MODELS:
            model, raw = head, rest
        elif head == "extended":
            extended, raw = True, rest
        else:
            break

    return model, extended, raw.strip()


def _attachment_note(*collected: Collected | None) -> str:
    """Generate short metadata labels of media attachments for conversation memory."""
    labels = [c.spec.label for c in collected if c and c.media is not None and c.spec]
    return f"\n[پیوست: {'، '.join(labels)}]" if labels else ""


def _notes_text(*collected: Collected | None) -> str:
    """Aggregate media warning notes into a display suffix."""
    notes = []
    for item in collected:
        if item:
            notes.extend(item.notes)
    return "\n\n" + "\n".join(notes) if notes else ""


async def _build_messages(chat_id: int, content) -> list[dict]:
    """Construct API payload message list including system instructions and chat memory."""
    settings = get_ai_settings(chat_id)

    system = BASE_SYSTEM_PROMPT
    if settings.get("persona"):
        system += f"\n\nADDITIONAL PERSONA INSTRUCTIONS:\n{settings['persona']}"

    messages = [{"role": "system", "content": system}]
    messages.extend(await get_ai_history(chat_id, config.AI_MAX_TURNS))
    messages.append({"role": "user", "content": content})
    return messages


async def _run_stream(
    client,
    message,
    *,
    content,
    header: str,
    memory_text: str | None,
    model: str | None = None,
    is_extended: bool = False,
    footer_note: str = "",
):
    """
    Execute a complete streaming LLM query and render the response into Telegram.

    Centralized pipeline for .ask, .r, .rp, and .sum: handles cancellation events,
    anchor message management, token streaming, memory persistence, and error routing.
    """
    chat_id = message.chat.id

    if chat_id in _stop_flags:
        await respond(message, "⏳ یک درخواست برای این چت در حال اجراست. `.aistop` برای توقف.")
        return

    stop_event = asyncio.Event()
    _stop_flags[chat_id] = stop_event

    editor = None
    stopped = False

    try:
        settings = get_ai_settings(chat_id)
        chosen = model or settings.get("model") or config.AI_MODEL

        placeholder = await respond(message, f"{header}⏳ در حال پردازش...")
        if placeholder is None:
            log.warning("Could not post placeholder message in chat %s; aborting", chat_id)
            return

        editor = StreamEditor(placeholder, header=header)
        messages = await _build_messages(chat_id, content)

        # `aclosing` ensures backend generator and semaphores are promptly cleaned up on cancellation
        async with aclosing(stream_chat(messages, model=chosen, is_extended=is_extended)) as stream:
            async for piece in stream:
                if stop_event.is_set():
                    stopped = True
                    break
                await editor.feed(piece)

        footer = footer_note
        if stopped:
            footer += "\n\n⏹ _متوقف شد._"
        await editor.finish(footer=footer)

        # Persist conversation memory only if a valid response was generated
        if memory_text and editor.full_text.strip():
            await add_ai_message(
                chat_id, "user", memory_text[:MEMORY_ITEM_LIMIT], config.AI_MAX_TURNS
            )
            await add_ai_message(
                chat_id, "assistant", editor.full_text[:MEMORY_ITEM_LIMIT], config.AI_MAX_TURNS
            )

    except AIError as e:
        log.warning("AI service error in chat %s: %s", chat_id, e)
        await _show_error(message, editor, friendly_error(e))

    except asyncio.CancelledError:
        raise

    except Exception as e:
        log.exception("Unexpected failure while streaming in chat %s", chat_id)
        await _show_error(message, editor, f"❌ خطای غیرمنتظره:\n{describe(e)}")

    finally:
        _stop_flags.pop(chat_id, None)


async def _show_error(message, editor, text: str) -> None:
    """Display error message on the anchor message, preserving partial output if available."""
    if editor is None:
        await respond(message, text, parse_mode=ParseMode.DISABLED)
        return

    if editor.full_text.strip():
        await editor.finish(footer=f"\n\n⚠️ {text}")
        return

    await editor.show(text)


# ==========================================================
#                      Primary Command: ask
# ==========================================================

@Client.on_message(AUTH_FILTER & filters.command("ask", prefixes=PREFIXES))
@safe_handler
async def ask_command(client, message):
    """
    Query the AI model with text, replied content, or attached media.

    Supported patterns:
    - Text prompt: `.ask How does Python async work?`
    - Media attachments: Send photo/audio/file with `.ask ...` in caption
    - Reply context: Reply to any message with `.ask summarize this`
    """
    model, is_extended, prompt = _extract_flags(strip_command(message))

    reply = message.reply_to_message
    own = await collect(client, message)
    replied = await collect(client, reply) if reply else None

    if not prompt:
        if replied and not replied.is_empty:
            prompt = "این پیام را بررسی کن و توضیح بده."
        elif own.media is not None:
            prompt = "این فایل را بررسی کن و محتوایش را توضیح بده."

    if not prompt:
        await respond(
            message,
            "❌ سوالت را بنویس.\n"
            "مثال: `.ask پایتون async چطور کار می‌کند؟`\n"
            "یا روی یک پیام ریپلای کن و `.ask خلاصه کن` بزن.\n"
            "عکس/ویس/فایل هم می‌توانی پیوست کنی.",
        )
        return

    content: list = []
    if replied and not replied.is_empty:
        content.append(quote_block(reply, replied.text, replied.spec.label if replied.spec else None))
        if replied.media is not None:
            content.append(replied.media)
    if own.media is not None:
        content.append(own.media)
    content.append(f"درخواست کاربر: {prompt}" if len(content) > 0 else prompt)

    await _run_stream(
        client,
        message,
        content=content,
        header=f"{prompt}\n\n",
        memory_text=prompt + _attachment_note(own, replied),
        model=model,
        is_extended=is_extended,
        footer_note=_notes_text(own, replied),
    )


# ==========================================================
#          Command r: Contextual Reply Inspection
# ==========================================================

@Client.on_message(AUTH_FILTER & filters.command(["r", "re", "about"], prefixes=PREFIXES))
@safe_handler
async def about_reply_command(client, message):
    """
    Ask a question specifically about a replied message.

    Usage:
        .r                      -> Analyze and explain the replied message
        .r What does this mean? -> Specific query regarding the replied message
        .r pro Summarize it     -> Specify model alias
    """
    reply = message.reply_to_message
    if not reply:
        await respond(
            message,
            "❌ روی پیامی که می‌خواهی درباره‌اش بپرسی ریپلای کن.\n"
            "مثال: `.r این عکس چیست؟` یا فقط `.r`\n"
            "عکس، ویس، ویدیو و فایل هم پشتیبانی می‌شود.",
        )
        return

    model, is_extended, question = _extract_flags(strip_command(message))

    status = await respond(message, "⏳ در حال آماده‌سازی پیام...")
    replied = await collect(client, reply)

    if replied.is_empty:
        await respond(
            status or message,
            "❌ این پیام نه متنی دارد نه فایلی که قابل بررسی باشد." + _notes_text(replied),
        )
        return

    if not question:
        question = (
            "این پیام را بررسی کن: منظور فرستنده را توضیح بده و اگر عکس، صدا یا "
            "فایلی دارد محتوایش را دقیق شرح بده."
        )

    content = [
        quote_block(reply, replied.text, replied.spec.label if replied.spec else None),
    ]
    if replied.media is not None:
        content.append(replied.media)
    content.append(f"درخواست کاربر درباره همین پیام: {question}")

    # Use status message as the streaming anchor to reduce message clutter
    anchor = status if status is not None else message
    await _run_stream(
        client,
        anchor,
        content=content,
        header=f"🔍 {question}\n\n" if len(question) < 200 else "🔍 بررسی پیام\n\n",
        memory_text=f"درباره پیام {sender_name(reply)}: {question}" + _attachment_note(replied),
        model=model,
        is_extended=is_extended,
        footer_note=_notes_text(replied),
    )


# ==========================================================
#          Command rp: Reply Suggestions Generator
# ==========================================================

@Client.on_message(AUTH_FILTER & filters.command(["rp", "suggest"], prefixes=PREFIXES))
@safe_handler
async def suggest_reply_command(client, message):
    """
    Suggest quick response options for a replied message.

    Usage:
        .rp             -> Generate 3 suggested replies
        .rp politely reject -> Suggest replies with specified tone
    """
    reply = message.reply_to_message
    if not reply:
        await respond(
            message,
            "❌ روی پیامی که می‌خواهی جوابش را بدهی ریپلای کن.\n"
            "مثال: `.rp` یا `.rp مودبانه رد کن`",
        )
        return

    model, is_extended, tone = _extract_flags(strip_command(message))

    status = await respond(message, "⏳ در حال آماده‌سازی پیشنهادها...")
    replied = await collect(client, reply)

    if replied.is_empty:
        await respond(
            status or message,
            "❌ این پیام محتوایی برای جواب دادن ندارد." + _notes_text(replied),
        )
        return

    instruction = (
        "سه جواب کوتاه و متفاوت برای این پیام پیشنهاد بده (به صورت لیست شماره‌دار). "
        "جواب‌ها باید به همان زبان پیام باشند و آماده کپی کردن — بدون توضیح اضافه."
    )
    if tone:
        instruction += f"\nلحن یا نکته مورد نظر کاربر: {tone}"

    content = [
        quote_block(reply, replied.text, replied.spec.label if replied.spec else None),
    ]
    if replied.media is not None:
        content.append(replied.media)
    content.append(instruction)

    anchor = status if status is not None else message
    await _run_stream(
        client,
        anchor,
        content=content,
        header="✍️ پیشنهاد جواب\n\n",
        memory_text=None,  # Suggestions are ephemeral; omitted from conversation memory
        model=model,
        is_extended=is_extended,
        footer_note=_notes_text(replied),
    )


# ==========================================================
#                 Command sum: Summarization
# ==========================================================

@Client.on_message(AUTH_FILTER & filters.command(["sum", "tldr"], prefixes=PREFIXES))
@safe_handler
async def summarize_command(client, message):
    """
    Summarize a single replied message or recent chat conversation history.

    Usage:
        .sum (on reply) -> Summarize replied message
        .sum            -> Summarize last 50 chat messages
        .sum 100        -> Summarize last 100 chat messages
    """
    model, is_extended, args = _extract_flags(strip_command(message))
    reply = message.reply_to_message

    count = None
    first = args.split(None, 1)[0] if args else ""
    if first.isdigit():
        count = int(first)
        if count < 1:
            await respond(message, "❌ تعداد پیام باید حداقل ۱ باشد.")
            return
        if count > SUM_MAX_MESSAGES:
            await respond(message, f"❌ حداکثر {SUM_MAX_MESSAGES} پیام قابل خلاصه‌سازی است.")
            return

    # Scenario 1: Summarize single replied message
    if reply and count is None:
        status = await respond(message, "⏳ در حال خلاصه‌سازی...")
        replied = await collect(client, reply)

        if replied.is_empty:
            await respond(
                status or message,
                "❌ این پیام محتوایی برای خلاصه کردن ندارد." + _notes_text(replied),
            )
            return

        content = [
            quote_block(reply, replied.text, replied.spec.label if replied.spec else None),
        ]
        if replied.media is not None:
            content.append(replied.media)
        content.append(
            "این پیام را خلاصه کن: نکات اصلی را در چند بولت کوتاه بنویس. "
            "اگر فایل صوتی یا تصویری دارد، محتوای آن را هم خلاصه کن."
        )

        anchor = status if status is not None else message
        await _run_stream(
            client,
            anchor,
            content=content,
            header="📝 خلاصه پیام\n\n",
            memory_text=None,
            model=model,
            is_extended=is_extended,
            footer_note=_notes_text(replied),
        )
        return

    # Scenario 2: Summarize last N chat messages
    count = count or SUM_DEFAULT_MESSAGES
    status = await respond(message, f"⏳ در حال خواندن {count} پیام آخر...")

    skip = {message.id}
    if status is not None:
        skip.add(status.id)

    history, found = await chat_history_text(client, message.chat.id, count, skip)

    if not found:
        await respond(status or message, "❌ پیامی برای خلاصه کردن پیدا نشد.")
        return

    content = [
        f"گفتگوی زیر مربوط به یک چت تلگرام است (از قدیم به جدید):\n---\n{history}\n---",
        "این گفتگو را خلاصه کن: موضوع‌های اصلی، تصمیم‌ها و کارهای باقی‌مانده را "
        "در چند بولت کوتاه بنویس. اگر سوال بی‌جواب مانده، جدا اشاره کن.",
    ]

    anchor = status if status is not None else message
    await _run_stream(
        client,
        anchor,
        content=content,
        header=f"📝 خلاصه {found} پیام آخر\n\n",
        memory_text=None,
        model=model,
        is_extended=is_extended,
    )


@Client.on_message(AUTH_FILTER & filters.command("aistop", prefixes=PREFIXES))
@safe_handler
async def aistop_command(client, message):
    """Signal cancellation for active streaming generation in the current chat."""
    event = _stop_flags.get(message.chat.id)
    if not event:
        await respond(message, "❌ هیچ درخواست در حال اجرایی برای این چت نیست.")
        return
    event.set()
    await respond(message, "⏹ درخواست متوقف شد.")


# ==========================================================
#               Administrative Commands: ai
# ==========================================================

_AI_USAGE = (
    "🤖 **دستیار هوش مصنوعی**\n\n"
    "`.ask <سوال>` — پرسیدن سوال (عکس/ویس/فایل هم می‌شود پیوست کرد)\n"
    "`.r <سوال>` — سوال درباره پیامی که رویش ریپلای کرده‌ای\n"
    "`.rp` — پیشنهاد چند جواب برای پیام طرف مقابل\n"
    "`.sum [تعداد]` — خلاصه یک پیام یا n پیام آخر چت\n"
    "`.aistop` — توقف پاسخ در حال تولید\n\n"
    "`.ai models` — فهرست مدل‌ها\n"
    "`.ai model <نام>` — تغییر مدل این چت\n"
    "`.ai persona <متن>` — تعیین شخصیت برای این چت\n"
    "`.ai persona clear` — حذف شخصیت\n"
    "`.ai reset` — پاک کردن حافظه گفتگوی این چت\n"
    "`.ai status` — وضعیت فعلی"
)


@Client.on_message(filters.me & filters.command("ai", prefixes=PREFIXES))
@safe_handler
async def ai_command(client, message):
    """Manage AI settings, personas, conversation memory, and model selections."""
    chat_id = message.chat.id

    raw = strip_command(message)
    pieces = raw.split(None, 1)
    sub = pieces[0].lower() if pieces else "help"
    arg = pieces[1].strip() if len(pieces) > 1 else ""

    # 1. Help Overview
    if sub in ("help", "h"):
        await respond(message, _AI_USAGE)
        return

    # 2. List Models
    if sub == "models":
        current = get_ai_settings(chat_id).get("model") or config.AI_MODEL
        lines = ["🧠 **نام‌های مستعار موجود:**\n"]
        for alias, real in MODELS.items():
            mark = " ⬅️" if real == resolve_model(current) else ""
            lines.append(f"`{alias}` → `{real}`{mark}")

        try:
            available = await list_models()
            main = [m for m in available if m.startswith("gemini-3") and "image" not in m]
            lines.append(f"\n📋 **مدل‌های اصلی سرویس ({len(available)} مدل در کل):**")
            lines.append(", ".join(f"`{m}`" for m in main[:25]))
        except AIError as e:
            log.warning("Could not list models: %s", e)
            lines.append("\n⚠️ فهرست کامل سرویس در دسترس نبود.")

        lines.append("\n_می‌توانی آیدی کامل هر مدل دیگری را هم مستقیم وارد کنی._")
        await respond(message, "\n".join(lines))
        return

    # 3. Switch Chat Model
    if sub == "model":
        if not arg:
            current = get_ai_settings(chat_id).get("model") or config.AI_MODEL
            await respond(
                message,
                f"🧠 مدل فعلی این چت: `{resolve_model(current)}`\nبرای تغییر: `.ai model flash`",
            )
            return

        resolved = resolve_model(arg)
        status = await respond(message, f"⏳ در حال بررسی مدل `{resolved}`...")

        try:
            available = await list_models()
        except AIError as e:
            log.warning("Model validation skipped: %s", e)
            await set_ai_model(chat_id, arg)
            await respond(
                status or message,
                f"⚠️ سرویس در دسترس نبود و مدل بدون بررسی ذخیره شد: `{resolved}`",
            )
            return

        if resolved not in available:
            await respond(
                status or message,
                f"❌ مدل `{resolved}` در این سرویس موجود نیست.\n"
                f"`.ai models` را ببین یا آیدی درست را وارد کن.",
            )
            return

        await set_ai_model(chat_id, arg)
        log.info("Chat %s switched AI model to %s", chat_id, resolved)
        await respond(status or message, f"✅ مدل این چت روی `{resolved}` تنظیم شد.")
        return

    # 4. Persona Configuration
    if sub == "persona":
        if not arg:
            current = get_ai_settings(chat_id).get("persona")
            await respond(
                message,
                f"🎭 شخصیت فعلی:\n{current}"
                if current
                else "🎭 شخصیتی تنظیم نشده است.\nمثال: `.ai persona مثل یک استاد دانشگاه توضیح بده`",
                parse_mode=ParseMode.DISABLED,
            )
            return
        if arg.lower() in ("clear", "off", "حذف"):
            await set_ai_persona(chat_id, None)
            await respond(message, "🎭 شخصیت حذف شد.")
            return
        await set_ai_persona(chat_id, arg)
        await respond(
            message,
            f"🎭 شخصیت این چت تنظیم شد:\n{arg}",
            parse_mode=ParseMode.DISABLED,
        )
        return

    # 5. Clear Memory
    if sub == "reset":
        removed = await clear_ai_history(chat_id)
        await respond(message, f"🧹 حافظه گفتگوی این چت پاک شد ({removed} پیام).")
        return

    # 6. Status Diagnostics
    if sub == "status":
        status = await respond(message, "⏳ در حال بررسی وضعیت...")
        settings = get_ai_settings(chat_id)
        model = settings.get("model") or config.AI_MODEL
        persona = settings.get("persona")
        turns = await count_ai_history(chat_id)

        try:
            available = await list_models()
            health = f"✅ در دسترس ({len(available)} مدل)"
        except AIError as e:
            log.info("AI service health check failed: %s", e)
            health = "❌ در دسترس نیست"

        await respond(
            status or message,
            f"🤖 **وضعیت دستیار هوش مصنوعی**\n\n"
            f"🧠 مدل: `{resolve_model(model)}`\n"
            f"🎭 شخصیت: {'دارد' if persona else '—'}\n"
            f"💾 حافظه: {turns} از {config.AI_MAX_TURNS} پیام\n"
            f"🔑 تعداد کلید API: {len(config.AI_API_KEYS)}\n"
            f"🌐 سرویس: {health}",
        )
        return

    await respond(message, f"❌ زیردستور `{sub}` ناشناخته است.\n\n{_AI_USAGE}")