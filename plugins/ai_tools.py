"""
AI Tools: Translation, optical character recognition (OCR), and image generation.

Unlike the conversational assistant (`plugins/ai_assistant.py`), these tools
provide deterministic, single-turn responses without streaming or persistent
chat context history.

This module also exports shared constants and helpers across AI plugins:
the language lookup map (`LANGUAGES`) and user-facing error formatting (`friendly_error`).
"""

import logging

from pyrogram import Client, filters
from pyrogram.enums import ParseMode

from plugins.general import AUTH_FILTER
from utils.ai_context import collect, strip_command, target_message
from utils.ai_engines import FAST_CONFIG, AIError, generate, generate_image
from utils.helpers import describe, respond, respond_long, safe_delete, safe_handler
from utils.media import named_buffer

log = logging.getLogger(__name__)

PREFIXES = ["/", "."]

# Common language alias mapping (supports standard ISO codes, English names, and Persian script).
# Unmatched prefixes are treated as part of the query text.
LANGUAGES = {
    "fa": "Persian", "farsi": "Persian", "persian": "Persian", "فا": "Persian", "فارسی": "Persian",
    "en": "English", "english": "English", "انگلیسی": "English",
    "ar": "Arabic", "arabic": "Arabic", "عربی": "Arabic",
    "tr": "Turkish", "turkish": "Turkish", "ترکی": "Turkish",
    "de": "German", "german": "German", "آلمانی": "German",
    "fr": "French", "french": "French", "فرانسوی": "French",
    "es": "Spanish", "spanish": "Spanish", "اسپانیایی": "Spanish",
    "ru": "Russian", "russian": "Russian", "روسی": "Russian",
    "zh": "Chinese", "chinese": "Chinese", "چینی": "Chinese",
    "ja": "Japanese", "japanese": "Japanese", "ژاپنی": "Japanese",
    "ko": "Korean", "korean": "Korean", "کره‌ای": "Korean",
    "it": "Italian", "italian": "Italian", "ایتالیایی": "Italian",
    "pt": "Portuguese", "portuguese": "Portuguese",
    "hi": "Hindi", "hindi": "Hindi", "هندی": "Hindi",
    "ur": "Urdu", "urdu": "Urdu", "اردو": "Urdu",
    "ku": "Kurdish", "kurdish": "Kurdish", "کردی": "Kurdish",
    "az": "Azerbaijani", "azeri": "Azerbaijani", "ترکی‌آذری": "Azerbaijani",
}

TRANSLATE_SYSTEM = (
    "You are a professional translator. Output ONLY the translation — no preface, "
    "no explanation, no quotes around it, no notes about what you did. "
    "Preserve the original line breaks, emoji, numbers, names, @mentions, links "
    "and code snippets exactly as they are. Match the register of the source "
    "(casual stays casual, formal stays formal). If the input contains an image or "
    "audio, first read/listen to it and translate the text it contains."
)

OCR_SYSTEM = (
    "You extract text from images and documents. Output ONLY the text you can read, "
    "keeping its original line structure and language. Do not translate, do not "
    "summarize, do not describe the image. If there is no readable text, reply "
    "exactly with: NO_TEXT"
)

# Maximum character limit for technical error details displayed to the user.
ERROR_DETAIL_LIMIT = 200


def friendly_error(error: AIError) -> str:
    """
    Convert backend AI errors into localized, human-readable Persian error messages.

    Markdown formatting is intentionally disabled on the output to prevent rendering
    syntax errors caused by unescaped characters in upstream error traces.
    """
    text = (error.message or "").lower()
    detail = (error.message or "").strip()[:ERROR_DETAIL_LIMIT]

    # --- Unconfigured API Key ---
    if "no api key configured" in text:
        return (
            "❌ کلید هوش مصنوعی تنظیم نشده است.\n"
            "یک کلید از aistudio.google.com/apikey بگیر و در فایل .env "
            "مقابل AI_API_KEYS بگذار."
        )

    # --- Quota Exceeded / Free Tier Limitations ---
    if error.status == 429 and ("limit: 0" in text or "quota" in text):
        return (
            "❌ سهمیه این مدل روی کلید فعلی صفر است.\n"
            "مدل‌های تولید تصویر گوگل در پلن رایگان در دسترس نیستند و "
            "به کلید با پلن پولی نیاز دارند."
        )

    if error.status == 429:
        return f"❌ سهمیه سرویس تمام شده است؛ کمی بعد دوباره تلاش کن.\n{detail}"

    # --- Invalid or Unauthorized API Key ---
    if error.status in (401, 403) or "api key not valid" in text:
        return (
            "❌ کلید هوش مصنوعی معتبر نیست یا دسترسی لازم را ندارد.\n"
            "مقدار AI_API_KEYS در فایل .env را بررسی کن."
        )

    # --- Model Not Found ---
    if error.status == 404:
        return (
            "❌ مدل انتخاب‌شده روی این سرویس وجود ندارد.\n"
            "با `.ai models` فهرست را ببین یا مدل را عوض کن."
        )

    # --- Safety Filter Triggered ---
    if "safety filter" in text:
        return "❌ سرویس این درخواست یا پاسخ را به دلیل فیلتر ایمنی متوقف کرد."

    # --- Network Failure ---
    if "network failure" in text or "timed out" in text:
        return "❌ ارتباط با سرویس هوش مصنوعی برقرار نشد؛ کمی بعد دوباره تلاش کن."

    return f"❌ خطای سرویس هوش مصنوعی:\n{detail or 'unknown error'}"


def _notes_suffix(collected) -> str:
    """Format media extraction warnings and metadata notes as an output suffix."""
    if collected and collected.notes:
        return "\n" + "\n".join(collected.notes)
    return ""


# ==========================================================
#                        Translation
# ==========================================================

@Client.on_message(AUTH_FILTER & filters.command(["tr", "translate"], prefixes=PREFIXES))
@safe_handler
async def translate_command(client, message):
    """
    Translate input text, replied message text, or extracted media content.

    Usage:
        .tr Hello world           -> Translates to Persian (auto-detected)
        .tr en این متن             -> Translates to English
        .tr de (on reply)         -> Translates replied content to German
        .tr (on reply)            -> Auto bidirectional toggle (Persian <-> English)
    """
    raw = strip_command(message)

    # Extract destination language if specified as the first token.
    target_lang = None
    pieces = raw.split(None, 1)
    if pieces and pieces[0].lower() in LANGUAGES:
        target_lang = LANGUAGES[pieces[0].lower()]
        raw = pieces[1].strip() if len(pieces) > 1 else ""

    reply = message.reply_to_message
    source_text = raw

    if not source_text and not reply:
        await respond(
            message,
            "❌ متنی برای ترجمه نیست.\n"
            "مثال: `.tr en سلام دنیا`\n"
            "یا روی یک پیام ریپلای کن و `.tr` بزن (عکس و ویس هم پشتیبانی می‌شود).",
        )
        return

    status = await respond(message, "⏳ در حال ترجمه...")

    collected = None
    if not source_text and reply is not None:
        collected = await collect(client, reply)
        source_text = collected.text
        if collected.is_empty:
            await respond(
                status or message,
                "❌ این پیام محتوایی برای ترجمه ندارد." + _notes_suffix(collected),
            )
            return

    if target_lang:
        instruction = f"Translate the following into {target_lang}."
    else:
        # Default bidirectional translation prompt: Persian <-> English.
        instruction = (
            "If the source is Persian, translate it into English. "
            "Otherwise translate it into Persian."
        )

    content: list = [instruction]
    if collected is not None and collected.media is not None:
        content.append(collected.media)
    if source_text:
        content.append(f"---\n{source_text}\n---")

    try:
        result = await generate(
            [
                {"role": "system", "content": TRANSLATE_SYSTEM},
                {"role": "user", "content": content},
            ],
            generation_config=FAST_CONFIG,
        )
    except AIError as e:
        log.warning("Translation failed: %s", e)
        await respond(status or message, friendly_error(e))
        return

    notes = _notes_suffix(collected)

    # Translation output contains unescaped text; disable Markdown to prevent parse failures.
    await respond_long(
        status or message,
        f"🌐 {result.text}{notes}",
        parse_mode=ParseMode.DISABLED,
    )


# ==========================================================
#             Optical Character Recognition (OCR)
# ==========================================================

@Client.on_message(AUTH_FILTER & filters.command(["ocr", "readimg"], prefixes=PREFIXES))
@safe_handler
async def ocr_command(client, message):
    """
    Extract text from an image, screenshot, or PDF document via optical recognition.

    Usage:
        Reply to an image/PDF with `.ocr`
        Or send an image with `.ocr` in the caption
    """
    target = target_message(message)
    status = await respond(message, "⏳ در حال خواندن متن...")

    collected = await collect(client, target)

    if collected.media is None:
        # Split for the same reason as .stt: a file that was there but could
        # not be fetched has its own reason to give, and telling the user to
        # reply to a file they *did* reply to only sends them in a circle.
        if collected.spec is not None:
            await respond(
                status or message,
                _notes_suffix(collected).strip()
                or "❌ آماده‌سازی فایل ناموفق بود؛ دوباره امتحان کن.",
            )
            return

        await respond(
            status or message,
            "❌ روی یک عکس یا فایل ریپلای کن (یا عکس را با کپشن `.ocr` بفرست).",
        )
        return

    try:
        result = await generate(
            [
                {"role": "system", "content": OCR_SYSTEM},
                {"role": "user", "content": ["متن داخل این فایل را استخراج کن.", collected.media]},
            ],
            generation_config=FAST_CONFIG,
        )
    except AIError as e:
        log.warning("OCR failed: %s", e)
        await respond(status or message, friendly_error(e))
        return

    if result.text.strip() == "NO_TEXT":
        await respond(status or message, "❌ متن قابل خواندنی در این فایل پیدا نشد.")
        return

    await respond_long(status or message, f"📄 {result.text}", parse_mode=ParseMode.DISABLED)


# ==========================================================
#                     Image Generation
# ==========================================================

@Client.on_message(AUTH_FILTER & filters.command(["img", "image", "draw"], prefixes=PREFIXES))
@safe_handler
async def image_command(client, message):
    """
    Generate an image from a textual prompt, or edit a replied image.

    Usage:
        .img An astronaut cat in watercolor style
        (On reply): `.img Add a red hat`

    Note:
        Google Imagen models require paid billing tier; free API keys will return quota errors.
    """
    prompt = strip_command(message)

    if not prompt:
        await respond(
            message,
            "❌ توضیح تصویر را بنویس.\n"
            "مثال: `.img یک گربه فضانورد، سبک آبرنگ`\n"
            "برای ویرایش، روی یک عکس ریپلای کن: `.img کلاه قرمز بهش اضافه کن`",
        )
        return

    reply = message.reply_to_message
    status = await respond(message, "🎨 در حال ساخت تصویر... (چند ثانیه)")

    images = []
    if reply is not None:
        collected = await collect(client, reply)
        if collected.media is not None and collected.spec and collected.spec.is_image:
            images.append(collected.media)

    try:
        result = await generate_image(prompt, images)
    except AIError as e:
        log.warning("Image generation failed: %s", e)
        await respond(status or message, friendly_error(e))
        return

    pictures = [m for m in result.media if m.mime_type.startswith("image/")]
    if not pictures:
        await respond(
            status or message,
            "❌ مدل تصویری برنگرداند."
            + (f"\n\n{result.text[:500]}" if result.text else ""),
            parse_mode=ParseMode.DISABLED,
        )
        return

    extension = "png" if "png" in pictures[0].mime_type else "jpg"
    caption = f"🎨 {prompt}"[:1024]

    try:
        await client.send_photo(
            message.chat.id,
            named_buffer(pictures[0].data, f"image.{extension}"),
            caption=caption,
            reply_to_message_id=message.id,
            parse_mode=ParseMode.DISABLED,
        )
    except Exception as e:
        # Upload might fail due to payload size or chat restrictions.
        log.warning("Could not upload the generated image: %s", describe(e))
        await respond(status or message, "❌ تصویر ساخته شد ولی ارسالش به تلگرام ممکن نشد.")
        return

    # Clean up transient status message.
    await safe_delete(client, status)