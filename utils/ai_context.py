"""
Turning a Telegram message into input the model can understand.

Every AI command (ask, r, sum, tr, ocr, stt) needs the same thing: "take this
message and the one it replies to, separate out their text and media, and get
them ready to send". This module is that layer, so no plugin has to write it
again.

No handler is registered here; helper functions only.
"""

import logging
from typing import NamedTuple

import config
from utils.ai_engines import MediaPart, make_media_part
from utils.media import MediaError, MediaSpec, download_bytes, gemini_mime, inspect_media

log = logging.getLogger(__name__)

# Ceiling on how much of each message's text is given to the model. A Telegram
# message caps at 4096 characters, but a file caption or forwarded text can be
# longer.
MAX_TEXT_CHARS = 8000

# Ceiling on the length of each line when summarising chat history.
MAX_HISTORY_LINE = 500


class Collected(NamedTuple):
    """Content extracted from one message."""
    text: str                     # the message's text or caption
    media: MediaPart | None       # media ready to send to the model
    spec: MediaSpec | None        # media details (for display and decisions)
    notes: list[str]              # displayable warnings (a too-large file, say)

    @property
    def is_empty(self) -> bool:
        return not self.text and self.media is None

    @property
    def parts(self) -> list:
        """The content as a parts list for ai_engines."""
        items: list = []
        if self.text:
            items.append(self.text)
        if self.media is not None:
            items.append(self.media)
        return items


def sender_name(message) -> str:
    """A message sender's display name."""
    user = getattr(message, "from_user", None)
    if user is not None:
        if getattr(user, "is_self", False):
            return "من"
        name = " ".join(x for x in (user.first_name, user.last_name) if x)
        return name or user.username or str(user.id)

    chat = getattr(message, "sender_chat", None)
    if chat is not None:
        return getattr(chat, "title", None) or "کانال"

    return "ناشناس"


def plain_text(message) -> str:
    """A message's text or caption, whichever is present."""
    if message is None:
        return ""
    return (getattr(message, "text", None) or getattr(message, "caption", None) or "")[:MAX_TEXT_CHARS]


def target_message(message):
    """
    The message a command operates on.

    The replied-to message when there is one, otherwise the command message
    itself. Nearly every tool (translation, OCR, speech-to-text) follows this
    same rule.
    """
    return getattr(message, "reply_to_message", None) or message


def strip_command(message) -> str:
    """
    The message text with the command itself removed.

    message.command is not used, because it splits the arguments on whitespace
    and destroys multi-line text — which matters for translation and tts. Here
    only the first word (the command) is stripped off.
    """
    text = plain_text(message)
    if not text:
        return ""
    parts = text.split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""


async def collect(client, message, *, with_media: bool = True,
                  max_bytes: int | None = None) -> Collected:
    """
    Extract a message's text and media.

    Media errors are never raised: if the file is too large or unsupported,
    the message text is still used and the reason comes back as a warning in
    notes. So a broken attachment degrades the command rather than killing it.
    """
    text = plain_text(message)
    spec = inspect_media(message)
    notes: list[str] = []
    media: MediaPart | None = None

    if spec is not None and with_media:
        try:
            mime = gemini_mime(spec)
            data = await download_bytes(client, message, max_bytes or config.AI_MEDIA_MAX_BYTES)
            media = await make_media_part(data, mime, spec.file_name or spec.kind)
        except MediaError as e:
            notes.append(str(e))
        except Exception as e:
            log.warning("could not prepare media for the model: %s", type(e).__name__)
            notes.append("❌ آماده‌سازی فایل برای ارسال به مدل ناموفق بود.")

    return Collected(text=text, media=media, spec=spec, notes=notes)


def quote_block(message, text: str | None = None, media_label: str | None = None) -> str:
    """
    Build a quote block from a message, to hand to the model.

    The sender's name is deliberately included in the text: the model needs to
    know whose words these are to answer "what did they say" or "how should I
    reply to this" correctly.
    """
    body = text if text is not None else plain_text(message)
    if not body and media_label:
        body = f"[{media_label}]"
    header = f"پیام از {sender_name(message)}"
    return f"{header}:\n---\n{body}\n---"


async def chat_history_text(client, chat_id: int, limit: int,
                            skip_ids=()) -> tuple[str, int]:
    """
    Read a chat's last n messages as "name: message" text.

    The result is ordered oldest to newest so the model reads the conversation
    in the right order. Media messages appear as their kind label ([photo],
    say), because in a bulk summary the file itself is neither necessary nor
    economical.
    """
    lines: list[str] = []
    skip = set(skip_ids)

    # Read a little past the limit to backfill service and empty messages.
    async for msg in client.get_chat_history(chat_id, limit=limit + len(skip) + 10):
        if getattr(msg, "service", None) or getattr(msg, "empty", None):
            continue
        if msg.id in skip:
            continue

        text = plain_text(msg)
        if not text:
            spec = inspect_media(msg)
            if spec is None:
                continue
            text = f"[{spec.label}]"

        lines.append(f"{sender_name(msg)}: {text[:MAX_HISTORY_LINE]}")
        if len(lines) >= limit:
            break

    lines.reverse()
    return "\n".join(lines), len(lines)
