"""
Audio utilities: Speech-to-Text (STT) and Text-to-Speech (TTS).

- `.stt`: Transcribes attached voice, audio, or video files using AI models.
- `.tts`: Synthesizes speech from text and dispatches as native Telegram voice messages
          (falls back to standard WAV audio if FFmpeg is unavailable).
"""

import logging

from pyrogram import Client, filters
from pyrogram.enums import ParseMode

import config
from plugins.ai_tools import LANGUAGES, friendly_error
from plugins.general import AUTH_FILTER
from utils.ai_context import collect, strip_command, target_message
from utils.ai_engines import FAST_CONFIG, AIError, generate, synthesize_speech
from utils.helpers import describe, respond, respond_long, safe_delete, safe_handler
from utils.media import named_buffer, parse_pcm_rate, pcm_to_ogg, pcm_to_wav

log = logging.getLogger(__name__)

PREFIXES = ["/", "."]

# Maximum character limit for TTS input to control latency, API quota, and audio duration
TTS_MAX_CHARS = 3000

# Predefined voice profiles for TTS synthesis
VOICES = {
    "kore": "Kore",            # Neutral and formal
    "puck": "Puck",            # Lively / animated
    "charon": "Charon",        # Calm and serious
    "fenrir": "Fenrir",        # Energetic
    "aoede": "Aoede",          # Soft and gentle
    "leda": "Leda",            # Youthful
    "orus": "Orus",            # Assertive / firm
    "zephyr": "Zephyr",        # Bright
    "callirrhoe": "Callirrhoe",
    "enceladus": "Enceladus",
}

STT_SYSTEM = (
    "You transcribe audio. Output ONLY the transcript in the language actually "
    "spoken — no preface, no speaker labels unless there are clearly several "
    "speakers, no timestamps, no commentary. Keep it verbatim but add normal "
    "punctuation so it is readable. If the audio has no intelligible speech, "
    "reply exactly with: NO_SPEECH"
)


def _notes_suffix(collected) -> str:
    """Format media collection warnings or notes to append to the response."""
    if collected and collected.notes:
        return "\n" + "\n".join(collected.notes)
    return ""


# ==========================================================
#                    Speech-to-Text (STT)
# ==========================================================

@Client.on_message(AUTH_FILTER & filters.command(["stt", "v2t", "voice2text"], prefixes=PREFIXES))
@safe_handler
async def speech_to_text_command(client, message):
    """
    Transcribe speech from a voice note, audio file, video, or video note.

    Usage:
        Reply to audio/video with `.stt`
        `.stt [lang]` -> Transcribe and append translation (e.g., `.stt en`)
    """
    raw = strip_command(message)
    translate_to = LANGUAGES.get(raw.split(None, 1)[0].lower()) if raw else None

    target = target_message(message)
    status = await respond(message, "⏳ در حال گوش دادن...")

    collected = await collect(client, target)
    spec = collected.spec

    # Two different failures that used to be reported as one. "reply to a
    # voice" was shown even when a voice *had* been replied to and only its
    # download had failed, so the answer contradicted itself: the refusal on
    # the first line and the real reason on the second. Checking the spec —
    # which describes the media without downloading it — separates them, and
    # checking it for None first keeps the old AttributeError guard.
    if spec is None or not (spec.is_audio or spec.is_video):
        await respond(status or message,
                      "❌ روی یک ویس، فایل صوتی یا ویدیو ریپلای کن."
                      + _notes_suffix(collected))
        return

    if collected.media is None:
        # The right kind of file, which never reached the model. collect()
        # does not raise; it leaves a finished Persian sentence in notes.
        await respond(status or message,
                      _notes_suffix(collected).strip()
                      or "❌ آماده‌سازی فایل صوتی ناموفق بود؛ دوباره امتحان کن.")
        return

    # Instructions precede media parts in English to prevent model hallucination/echoing
    instruction = "Transcribe the attached audio."
    if translate_to:
        instruction += (f" Then add a blank line, the marker «---», another blank line, "
                        f"and the {translate_to} translation of the transcript.")

    try:
        result = await generate(
            [{"role": "system", "content": STT_SYSTEM},
             {"role": "user", "content": [instruction, collected.media]}],
            generation_config=FAST_CONFIG,
        )
    except AIError as e:
        log.warning("speech-to-text failed: %s", e)
        await respond(status or message, friendly_error(e))
        return

    if result.text.strip() == "NO_SPEECH":
        await respond(status or message, "❌ گفتار قابل تشخیصی در این فایل نبود.")
        return

    duration = collected.spec.duration
    header = f"🎙 متن پیاده‌شده ({duration} ثانیه):\n\n" if duration else "🎙 متن پیاده‌شده:\n\n"

    # Untrusted transcription content: Markdown/HTML parsing is disabled to prevent formatting errors
    await respond_long(status or message, header + result.text, parse_mode=ParseMode.DISABLED)


# ==========================================================
#                    Text-to-Speech (TTS)
# ==========================================================

@Client.on_message(AUTH_FILTER & filters.command(["tts", "say"], prefixes=PREFIXES))
@safe_handler
async def text_to_speech_command(client, message):
    """
    Synthesize speech from input text or replied message.

    Usage:
        .tts [text]
        .tts [voice_name] [text] -> Specify voice persona
        Reply to a message with `.tts` -> Read text from target message
        .tts voices -> List available voice personas
    """
    raw = strip_command(message)

    if raw.lower() in ("voices", "voice", "صداها"):
        names = "\n".join(f"`{v}`" for v in VOICES.values())
        await respond(message, f"🎧 **صداهای موجود:**\n{names}\n\n"
                               f"صدای پیش‌فرض: `{config.AI_TTS_VOICE}`\n"
                               f"مثال: `.tts puck سلام رفیق`")
        return

    # Extract optional voice selector if the first word matches a known voice key
    voice = None
    pieces = raw.split(None, 1)
    if pieces and pieces[0].lower() in VOICES:
        voice = VOICES[pieces[0].lower()]
        raw = pieces[1].strip() if len(pieces) > 1 else ""

    text = raw
    if not text and message.reply_to_message is not None:
        replied = await collect(client, message.reply_to_message, with_media=False)
        text = replied.text

    if not text:
        await respond(
            message,
            "❌ متنی برای خواندن نیست.\n"
            "مثال: `.tts سلام! حالت چطوره؟`\n"
            "یا روی یک پیام ریپلای کن و `.tts` بزن.\n"
            "`.tts voices` فهرست صداها را نشان می‌دهد."
        )
        return

    if len(text) > TTS_MAX_CHARS:
        await respond(message, f"❌ متن طولانی‌تر از {TTS_MAX_CHARS} کاراکتر است "
                               f"({len(text)} کاراکتر). کوتاهش کن.")
        return

    status = await respond(message, "🎧 در حال ساخت ویس...")

    try:
        part = await synthesize_speech(text, voice)
    except AIError as e:
        log.warning("text-to-speech failed: %s", e)
        await respond(status or message, friendly_error(e))
        return

    rate = parse_pcm_rate(part.mime_type)

    # Native Telegram voice notes require OGG/Opus. Fallback to WAV audio file if FFmpeg is absent.
    ogg = await pcm_to_ogg(part.data, rate)
    caption = text[:200] + ("…" if len(text) > 200 else "")

    try:
        if ogg:
            await client.send_voice(
                message.chat.id,
                named_buffer(ogg, "voice.ogg"),
                caption=caption,
                reply_to_message_id=message.id,
                parse_mode=ParseMode.DISABLED,
            )
        else:
            wav = pcm_to_wav(part.data, rate)
            await client.send_audio(
                message.chat.id,
                named_buffer(wav, "speech.wav"),
                caption=caption,
                reply_to_message_id=message.id,
                parse_mode=ParseMode.DISABLED,
            )
    except Exception as e:
        # Handle dispatch/upload failures (file size, chat restrictions, network errors)
        log.warning("could not upload the synthesized audio: %s", describe(e))
        await respond(status or message, "❌ ویس ساخته شد ولی ارسالش به تلگرام ممکن نشد.")
        return

    await safe_delete(client, status)