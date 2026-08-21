"""
Transport layer for Google's AI service (the Gemini API).

This module is responsible for the network only and has no dependency on
Pyrogram, so it can be tested apart from Telegram.

Features:
- Text streaming (streamGenerateContent + SSE)
- Non-streaming responses (generateContent), including media output
- Multimodal input: images, audio, video, PDF and text (inline or Files API)
- Image generation and text-to-speech
- Automatic rotation across several API keys

Language rule: this module produces no strings for direct display to the user.
The AIError message and every log line are **English** (Google's own error
text is English too); the plugin layer wraps it in a Persian sentence and
fully translates the common cases (plugins/ai_tools.friendly_error).
"""

import asyncio
import base64
import json
import logging
import random
from typing import NamedTuple

import aiohttp

import config

log = logging.getLogger(__name__)

API_ROOT = "https://generativelanguage.googleapis.com"
API_VERSION = "v1beta"

# --- Model aliases ---
# The names are verified against the service's live model list. Note: "pro"
# and the image models have a quota of zero on free-plan keys and return 429;
# the alias is kept correct so they work on a paid plan with no code change.
MODELS = {
    "flash": "gemini-3.6-flash",
    "flash37": "gemini-3.7-flash",
    "lite": "gemini-3.1-flash-lite",
    "pro": "gemini-3.1-pro-preview",
    "image": "gemini-3.1-flash-image",
    "tts": "gemini-3.1-flash-tts-preview",
}

# Index of the Google key currently in use.
_current_key_idx = 0

# Status codes where retrying makes sense.
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}

# Codes meaning "the problem is this key, not the request": quota exhausted,
# invalid key, or no access. Only these rotate to the next key. 400 is
# deliberately absent: it means *our* request is malformed, and rotating would
# just repeat the failure while hiding the real error message.
KEY_ROTATE_STATUS = {401, 403, 429}

MAX_RETRIES = 3

# Ceiling on the wait between attempts.
# The number is deliberately small: Telegram commands are interactive and the
# user should not sit in front of a "thinking…" message for 60 seconds. During
# an outage the service sends large Retry-After values, which are unsuitable
# for interactive work, so those are capped at this ceiling too.
MAX_BACKOFF = 8.0

# Size ceiling for a file sent inline (base64, inside the request itself).
# Google's limit on the total request is 20 MB and base64 inflates by about
# 33%, so the safe raw boundary is around 14 MB. Larger files are uploaded
# through the Files API.
MAX_INLINE_BYTES = 14 * 1024 * 1024

# How long to wait for an uploaded file to be processed (video takes seconds).
FILE_ACTIVE_TIMEOUT = 90.0
FILE_POLL_INTERVAL = 1.5

# Configuration for tool-style work (translation, OCR, speech-to-text).
# These have one short, definite answer and deep reasoning only adds latency;
# measured: the same request with a low thinkingLevel takes about half as long.
FAST_CONFIG = {"thinkingConfig": {"thinkingLevel": "low"}}

# Finish reasons meaning the model deliberately produced nothing.
BLOCKING_FINISH_REASONS = {
    "SAFETY", "PROHIBITED_CONTENT", "RECITATION", "BLOCKLIST",
    "SPII", "IMAGE_SAFETY", "LANGUAGE",
}

# --- Shared session ---
_session: aiohttp.ClientSession | None = None
_session_lock = asyncio.Lock()

# Limits how many requests run concurrently.
_semaphore: asyncio.Semaphore | None = None


class MediaPart(NamedTuple):
    """
    One piece of media, on the way to or from the model.

    It carries either raw data (inline) or the URI of a file uploaded through
    the Files API.
    """
    mime_type: str
    data: bytes = b""
    uri: str | None = None


class GenResult(NamedTuple):
    """The result of a non-streaming request."""
    text: str
    media: list[MediaPart]
    finish_reason: str | None = None


class AIError(Exception):
    """
    An error from the AI service.

    message is deliberately **English**: it is either Google's own text or our
    technical explanation, and its destination is the log and bug reports.
    Display to the user goes through plugins/ai_tools.friendly_error, which
    wraps it in a Persian sentence.
    """

    def __init__(self, message: str, status: int | None = None, retryable: bool = False,
                 retry_after: float | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.retryable = retryable
        self.retry_after = retry_after

    def __str__(self):
        # Only real error codes are shown. The service sometimes sends errors
        # inside the stream under a 200 status, and printing "[200]" as an
        # error code would just confuse the reader.
        if self.status and self.status >= 400:
            return f"[{self.status}] {self.message}"
        return self.message


def resolve_model(name: str | None) -> str:
    """
    Turn an alias into a real model id.

    A name that is not in the alias table is returned unchanged, so the user
    can pass any of the service's model ids directly.
    """
    if not name:
        name = config.AI_MODEL
    return MODELS.get(name.strip().lower(), name.strip())


def get_semaphore() -> asyncio.Semaphore:
    """The shared semaphore that bounds concurrent requests."""
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(config.AI_MAX_CONCURRENCY)
    return _semaphore


async def get_session() -> aiohttp.ClientSession:
    """
    Create (or reuse) the shared aiohttp session.

    The session has to be built inside the running event loop, which is why it
    is created lazily rather than at module level.

    An important note on the timeout: total is deliberately None. Set to a
    fixed number it would guillotine long responses mid-stream. sock_read
    handles the real case instead — the connection drops if the server sends
    nothing for a while.
    """
    global _session
    if _session is None or _session.closed:
        async with _session_lock:
            # Double-checked locking.
            if _session is None or _session.closed:
                _session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(
                        total=None,
                        sock_connect=15,
                        sock_read=config.AI_TIMEOUT,
                    ),
                    connector=aiohttp.TCPConnector(limit=20, ttl_dns_cache=300),
                )
                log.info("opened the shared aiohttp session for the AI service")
    return _session


async def close_session():
    """Close the session cleanly during shutdown."""
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
        log.info("closed the shared aiohttp session")
    _session = None


def _backoff_delay(attempt: int, retry_after: float | None = None) -> float:
    """
    Compute the wait between attempts (exponential plus jitter).

    The server's Retry-After is honoured, but capped at MAX_BACKOFF so the
    user is not left waiting behind an interactive command.
    """
    if retry_after is not None:
        return min(retry_after, MAX_BACKOFF)
    return min(2 ** attempt + random.uniform(0, 1), MAX_BACKOFF)


def _parse_retry_after(resp) -> float | None:
    """
    Read the Retry-After header — for 429 (rate limiting) only.

    On a 429 the server genuinely knows better than we do how long to wait,
    but on 5xx errors this header is usually a large fixed number, and
    honouring it would make a simple command take tens of seconds.
    """
    if resp.status != 429:
        return None
    raw = resp.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


async def _extract_error(resp) -> str:
    """Extract the error message from a response body, JSON or plain text."""
    try:
        body = await resp.text()
    except Exception:
        return f"HTTP {resp.status} with an unreadable body"

    try:
        data = json.loads(body)
        if isinstance(data, dict) and "error" in data:
            err = data["error"]
            if isinstance(err, dict):
                return err.get("message") or str(err)
            return str(err)
    except (json.JSONDecodeError, TypeError):
        pass

    return (body[:300] or f"HTTP {resp.status} with an empty body").strip()


def _raise_for_status(resp, message: str):
    """Build the standard AIError from a failed response."""
    raise AIError(
        message,
        status=resp.status,
        retryable=resp.status in RETRYABLE_STATUS,
        retry_after=_parse_retry_after(resp),
    )


# ==========================================================
#                      Key management
# ==========================================================

def _keys() -> list[str]:
    keys = [k for k in config.AI_API_KEYS if k]
    if not keys:
        raise AIError("no API key configured (set AI_API_KEYS in .env)")
    return keys


def _rotate_key(keys: list[str], reason) -> None:
    global _current_key_idx
    _current_key_idx = (_current_key_idx + 1) % len(keys)
    log.warning("rotated AI key because of %s; now using key #%d", reason, _current_key_idx)


async def _run_with_keys(attempt_fn):
    """
    Run a non-streaming request with key rotation and retries.

    attempt_fn is a coroutine that takes a key and returns a result. The
    policy: a key-related error (quota/access) moves to the next key, but only
    for as many keys as exist; past that, rotating is pointless and we switch
    to exponential backoff to give the service room to breathe.
    """
    global _current_key_idx
    keys = _keys()
    total_attempts = MAX_RETRIES * len(keys)
    rotations = 0
    last_error: AIError | None = None

    async with get_semaphore():
        for attempt in range(total_attempts):
            try:
                return await attempt_fn(keys[_current_key_idx % len(keys)])

            except AIError as e:
                last_error = e
                if e.status in KEY_ROTATE_STATUS and rotations < len(keys) - 1:
                    rotations += 1
                    _rotate_key(keys, e.status)
                    continue
                if not e.retryable or attempt >= total_attempts - 1:
                    raise
                await asyncio.sleep(_backoff_delay(attempt, e.retry_after))

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_error = AIError(f"network failure: {type(e).__name__}", retryable=True)
                if attempt >= total_attempts - 1:
                    raise last_error
                await asyncio.sleep(_backoff_delay(attempt))

    raise last_error or AIError("request failed with no further detail")


# ==========================================================
#          Converting messages into Gemini's structure
# ==========================================================

def _content_to_parts(content) -> list[dict]:
    """
    Convert one message's content into Google's parts array.

    The content may be a plain string (text mode) or a list of strings and
    MediaParts (multimodal mode). Conversation memory is always a string, so
    this function has to accept both.
    """
    if isinstance(content, str):
        return [{"text": content}] if content else []

    parts = []
    for item in content:
        if isinstance(item, MediaPart):
            if item.uri:
                parts.append({"fileData": {"mimeType": item.mime_type, "fileUri": item.uri}})
            else:
                parts.append({"inlineData": {
                    "mimeType": item.mime_type,
                    "data": base64.b64encode(item.data).decode(),
                }})
        elif item:
            parts.append({"text": str(item)})
    return parts


def _build_payload(messages, is_extended: bool = False,
                   generation_config: dict | None = None) -> dict:
    """Build the request body from OpenAI-style messages."""
    contents = []
    system_texts = []

    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")

        if role == "system":
            if isinstance(content, str):
                system_texts.append(content)
            continue

        parts = _content_to_parts(content)
        if not parts:
            continue
        contents.append({"role": "model" if role == "assistant" else "user", "parts": parts})

    if is_extended:
        # Gemini has no boolean flag for this mode, so it is applied as a
        # system instruction.
        system_texts.append(
            "[EXTENDED MODE]: Provide a highly detailed, deeply analyzed, "
            "and step-by-step reasoning response."
        )

    payload: dict = {"contents": contents}
    if system_texts:
        payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_texts)}]}
    if generation_config:
        payload["generationConfig"] = generation_config
    return payload


def _collect_parts(candidate: dict) -> tuple[str, list[MediaPart]]:
    """
    Separate text and media out of one candidate.

    Parts flagged as thought (the model's internal reasoning summary) are
    dropped; they are not the user's answer and showing them only clutters
    the message.
    """
    texts: list[str] = []
    media: list[MediaPart] = []

    for part in (candidate.get("content") or {}).get("parts") or []:
        if part.get("thought"):
            continue
        if "text" in part:
            texts.append(part["text"])
        elif "inlineData" in part:
            inline = part["inlineData"]
            try:
                media.append(MediaPart(inline.get("mimeType", ""),
                                       base64.b64decode(inline.get("data", ""))))
            except (ValueError, TypeError):
                log.warning("could not base64-decode media returned by the model")

    return "".join(texts), media


def _check_prompt_block(data: dict):
    """Raise a clear error if the prompt itself was rejected by the safety filter."""
    feedback = data.get("promptFeedback") or {}
    reason = feedback.get("blockReason")
    if reason:
        raise AIError(f"prompt blocked by the safety filter ({reason})")


# ==========================================================
#                       Streaming mode
# ==========================================================

async def _stream_once(messages, model, api_key, is_extended=False, generation_config=None):
    session = await get_session()
    url = f"{API_ROOT}/{API_VERSION}/models/{model}:streamGenerateContent?alt=sse"
    payload = _build_payload(messages, is_extended, generation_config)

    produced = False
    finish_reason = None

    async with session.post(url, json=payload, headers={"x-goog-api-key": api_key}) as resp:
        if resp.status >= 400:
            _raise_for_status(resp, await _extract_error(resp))

        buf = b""
        async for chunk in resp.content.iter_chunked(1024):
            buf += chunk
            while b"\n" in buf:
                raw_line, buf = buf.split(b"\n", 1)
                line = raw_line.strip()

                if not line or not line.startswith(b"data: "):
                    continue

                data_str = line[6:].strip()
                if data_str == b"[DONE]":
                    return

                try:
                    obj = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                # Errors sometimes arrive inside the stream under a 200 status.
                if isinstance(obj, dict) and obj.get("error"):
                    err = obj["error"]
                    msg = err.get("message") if isinstance(err, dict) else str(err)
                    status = err.get("code") if isinstance(err, dict) else None
                    raise AIError(msg or "unspecified service error", status=status,
                                  retryable=status in RETRYABLE_STATUS)

                _check_prompt_block(obj)

                for candidate in obj.get("candidates", []):
                    text, _ = _collect_parts(candidate)
                    if candidate.get("finishReason"):
                        finish_reason = candidate["finishReason"]
                    if text:
                        produced = True
                        yield text

    # If the model produced no text and the finish reason was not a normal
    # stop, silence would be misleading: the user needs to know a safety
    # filter blocked the answer.
    if not produced and finish_reason in BLOCKING_FINISH_REASONS:
        raise AIError(f"generation stopped by the safety filter ({finish_reason})")


async def stream_chat(messages, model=None, is_extended=False, generation_config=None, **kwargs):
    """
    Stream the model's response chunk by chunk.

    The key rule: once a chunk has been delivered, no retry happens — a retry
    would duplicate text into the user's message.
    """
    global _current_key_idx
    keys = _keys()
    model = resolve_model(model)
    total_attempts = MAX_RETRIES * len(keys)
    rotations = 0
    last_error: AIError | None = None

    async with get_semaphore():
        for attempt in range(total_attempts):
            produced = False
            key = keys[_current_key_idx % len(keys)]

            try:
                async for piece in _stream_once(messages, model, key, is_extended, generation_config):
                    produced = True
                    yield piece
                return

            except AIError as e:
                last_error = e
                if not produced and e.status in KEY_ROTATE_STATUS and rotations < len(keys) - 1:
                    rotations += 1
                    _rotate_key(keys, e.status)
                    continue

                if produced or not e.retryable or attempt >= total_attempts - 1:
                    raise

                await asyncio.sleep(_backoff_delay(attempt, e.retry_after))

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                wrapped = AIError(f"network failure: {type(e).__name__}", retryable=True)
                last_error = wrapped
                if produced or attempt >= total_attempts - 1:
                    raise wrapped
                await asyncio.sleep(_backoff_delay(attempt))

    if last_error:
        raise last_error


# ==========================================================
#                  Normal (non-streaming) mode
# ==========================================================

async def generate(messages, model=None, is_extended=False,
                   generation_config=None) -> GenResult:
    """
    Get the whole response at once (text, plus media when present).

    Suited to short tool-style work such as translation, OCR and
    speech-to-text; for those, streaming only adds message-editing overhead.
    """
    try:
        return await _generate(messages, model, is_extended, generation_config)
    except AIError as e:
        # thinkingLevel only exists on generation-3 models. If the user has
        # pointed the model at an older generation the request is rejected
        # with a 400; rather than failing the command, retry once without it.
        if (e.status == 400 and generation_config
                and "thinkingConfig" in generation_config
                and "think" in e.message.lower()):
            log.info("model rejected thinkingConfig; retrying once without it")
            trimmed = {k: v for k, v in generation_config.items() if k != "thinkingConfig"}
            return await _generate(messages, model, is_extended, trimmed or None)
        raise


async def _generate(messages, model=None, is_extended=False,
                    generation_config=None) -> GenResult:
    resolved = resolve_model(model)
    url = f"{API_ROOT}/{API_VERSION}/models/{resolved}:generateContent"
    payload = _build_payload(messages, is_extended, generation_config)

    async def attempt(api_key: str) -> GenResult:
        session = await get_session()
        async with session.post(url, json=payload, headers={"x-goog-api-key": api_key}) as resp:
            if resp.status >= 400:
                _raise_for_status(resp, await _extract_error(resp))

            try:
                data = await resp.json()
            except (aiohttp.ContentTypeError, json.JSONDecodeError):
                raise AIError("response body could not be parsed as JSON", status=resp.status, retryable=True)

        _check_prompt_block(data)

        candidates = data.get("candidates") or []
        if not candidates:
            raise AIError("service returned no candidates")

        candidate = candidates[0]
        text, media = _collect_parts(candidate)
        finish = candidate.get("finishReason")

        if not text.strip() and not media:
            if finish in BLOCKING_FINISH_REASONS:
                raise AIError(f"generation stopped by the safety filter ({finish})")
            raise AIError("service returned an empty response")

        return GenResult(text.strip(), media, finish)

    return await _run_with_keys(attempt)


async def chat(messages, model=None, **kwargs) -> str:
    """A simplified generate() that returns the text only."""
    result = await generate(messages, model=model, **kwargs)
    return result.text


# ==========================================================
#                   File upload (Files API)
# ==========================================================

async def upload_file(data: bytes, mime_type: str, display_name: str = "file") -> MediaPart:
    """
    Upload a large file to the Files API and return a reference to it.

    Small files are sent inline; this path is only for anything larger than
    MAX_INLINE_BYTES. Uploaded files are deleted automatically after 48 hours,
    so no manual cleanup is needed.
    """

    async def attempt(api_key: str) -> MediaPart:
        session = await get_session()
        headers = {
            "x-goog-api-key": api_key,
            "X-Goog-Upload-Protocol": "raw",
            "X-Goog-Upload-File-Name": display_name,
            "Content-Type": mime_type,
        }
        async with session.post(f"{API_ROOT}/upload/{API_VERSION}/files",
                                headers=headers, data=data) as resp:
            if resp.status >= 400:
                _raise_for_status(resp, await _extract_error(resp))
            body = await resp.json()

        info = body.get("file") or {}
        uri, name = info.get("uri"), info.get("name")
        if not uri:
            raise AIError("file upload failed: no file URI in the response")

        # Video and large files start out in the PROCESSING state and cannot
        # be used in a request until they become ACTIVE.
        state = info.get("state")
        waited = 0.0
        while state == "PROCESSING" and waited < FILE_ACTIVE_TIMEOUT:
            await asyncio.sleep(FILE_POLL_INTERVAL)
            waited += FILE_POLL_INTERVAL
            async with session.get(f"{API_ROOT}/{API_VERSION}/{name}",
                                   headers={"x-goog-api-key": api_key}) as resp:
                if resp.status >= 400:
                    _raise_for_status(resp, await _extract_error(resp))
                info = await resp.json()
            state = info.get("state")

        if state == "FAILED":
            raise AIError("the service failed to process the uploaded file")
        if state == "PROCESSING":
            raise AIError("the uploaded file is still processing after the timeout")

        return MediaPart(info.get("mimeType", mime_type), b"", uri)

    return await _run_with_keys(attempt)


async def make_media_part(data: bytes, mime_type: str, display_name: str = "file") -> MediaPart:
    """Build a MediaPart from raw data: inline when small, Files API when large."""
    if len(data) <= MAX_INLINE_BYTES:
        return MediaPart(mime_type, data)
    log.info("uploading %s bytes through the Files API", len(data))
    return await upload_file(data, mime_type, display_name)


# ==========================================================
#                 Image and audio generation
# ==========================================================

async def generate_image(prompt: str, images: list[MediaPart] | None = None,
                         model: str | None = None) -> GenResult:
    """
    Generate or edit an image.

    When images are supplied the model edits them ("add a hat to this", say).
    """
    content: list = [prompt]
    content.extend(images or [])
    messages = [{"role": "user", "content": content}]
    return await generate(messages, model=model or config.AI_IMAGE_MODEL)


async def synthesize_speech(text: str, voice: str | None = None,
                            model: str | None = None) -> MediaPart:
    """
    Convert text to speech.

    The result is raw 16-bit PCM (a mime like `audio/L16;rate=24000`);
    converting it into something Telegram can play is the caller's job.
    """
    result = await generate(
        [{"role": "user", "content": text}],
        model=model or config.AI_TTS_MODEL,
        generation_config={
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {
                    "prebuiltVoiceConfig": {"voiceName": voice or config.AI_TTS_VOICE}
                }
            },
        },
    )

    for part in result.media:
        if part.mime_type.lower().startswith("audio/"):
            return part

    raise AIError("the service returned no audio part")


# ==========================================================
#                        Model listing
# ==========================================================

async def list_models() -> list[str]:
    """Fetch the list of models usable for content generation."""

    async def attempt(api_key: str) -> list[str]:
        session = await get_session()
        async with session.get(f"{API_ROOT}/{API_VERSION}/models?pageSize=200",
                               headers={"x-goog-api-key": api_key}) as resp:
            if resp.status >= 400:
                _raise_for_status(resp, await _extract_error(resp))
            data = await resp.json()

        names = []
        for model in data.get("models", []):
            name = (model.get("name") or "").removeprefix("models/")
            methods = model.get("supportedGenerationMethods") or []
            if name and "generateContent" in methods:
                names.append(name)
        return sorted(names)

    return await _run_with_keys(attempt)
