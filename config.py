"""
Configuration loading from the environment (.env) with fail-fast validation.

Language rule: the error and warning messages in this file are **English**.
They appear before the bot is up and are read in a terminal, not in a Telegram
chat, and they are the first thing a new user sees during setup.

There are two kinds of variable and their behaviour differs on purpose:

  * **Required** (API_ID, API_HASH): a missing value raises ConfigError and
    the bot does not start at all. Running without them is impossible.

  * **Optional**: an invalid value only logs a warning and the default is
    used. A typo in `AI_TIMEOUT` must not take the whole bot down.
"""

import logging
import os

from dotenv import load_dotenv

# Load the variables from .env.
# (main.py calls this too, so that LOG_LEVEL is available before logging is
#  configured; the operation is idempotent and repeating it is harmless.)
load_dotenv()

log = logging.getLogger("config")


class ConfigError(Exception):
    """Missing or invalid configuration — the bot cannot start."""


def _require(name: str) -> str:
    """Read a required environment variable (raises when empty)."""
    value = os.getenv(name, "")
    if not value.strip():
        raise ConfigError(
            f"{name} is not set. Copy .env.example to .env and fill it in "
            f"(get API_ID/API_HASH from https://my.telegram.org)."
        )
    return value.strip()


def _require_int(name: str) -> int:
    """Read a required integer environment variable."""
    raw = _require(name)
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from None


def _optional_int(name: str, default: int, minimum: int | None = None,
                  maximum: int | None = None) -> int:
    """
    Read an optional integer variable with a default and bounds.

    The bounds are not cosmetic: AI_MAX_CONCURRENCY=0, for instance, used to
    build an asyncio.Semaphore(0) and park *every* AI command behind it
    forever, with no error anywhere to point at the cause.
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        value = default
    else:
        try:
            value = int(raw)
        except ValueError:
            log.warning("%s must be an integer, got %r; using %s", name, raw, default)
            value = default

    if minimum is not None and value < minimum:
        log.warning("%s=%s is below the minimum; using %s", name, value, minimum)
        value = minimum
    if maximum is not None and value > maximum:
        log.warning("%s=%s is above the maximum; using %s", name, value, maximum)
        value = maximum

    return value


def _optional_bool(name: str, default: bool) -> bool:
    """Read an optional boolean variable (1/true/yes/on vs 0/false/no/off)."""
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False

    log.warning("%s must be true or false, got %r; using %s", name, raw, default)
    return default


# --- Telegram settings ---
# API_ID has to be converted to an integer.
API_ID = _require_int("API_ID")
API_HASH = _require("API_HASH")

# api_hash is always 32 hex characters. When it is not, Telegram answers the
# connection attempt with an opaque error; this warning sits right above that
# error and explains it. It is deliberately a warning and not an error: if the
# format ever changes, it must not take down a working bot.
if len(API_HASH) != 32 or not all(c in "0123456789abcdefABCDEF" for c in API_HASH):
    log.warning(
        "API_HASH does not look like a 32-character hex string; "
        "double-check the value copied from my.telegram.org"
    )

# --- Clock synchronisation ---
# MTProto derives msg_id from the client clock; a clock that runs behind earns
# a BadMsgNotification[16] and the session stops working. Inside a container
# the system clock cannot be set, so the offset is read from NTP and applied to
# msg_id generation only. If the network blocks UDP/123, the system clock is
# used silently.
CLOCK_SYNC = _optional_bool("CLOCK_SYNC", True)

# Interval between periodic measurements (seconds). VM clocks drift over time,
# so measuring once at startup is not enough. The floor of 60 s keeps a very
# small value from turning the bot into a high-traffic NTP client.
CLOCK_SYNC_INTERVAL = _optional_int("CLOCK_SYNC_INTERVAL", 1800, minimum=60)

# How long to wait for the time servers to answer (seconds). This value is
# added directly to startup latency, so it is kept short.
CLOCK_SYNC_TIMEOUT = _optional_int("CLOCK_SYNC_TIMEOUT", 5, minimum=1, maximum=60)

# An offset below this value (seconds) is not applied at all: Telegram
# tolerates roughly 30 s, and adjusting msg_id without cause is a pointless
# risk. The number also has to sit above the HTTP path's measurement error
# (about half a second), or a healthy clock would keep oscillating across the
# threshold.
CLOCK_SYNC_THRESHOLD = 5.0

# --- TTL media guard (self-destructing / view-once) ---
# Timed private-chat media is downloaded before it is "viewed" and kept in
# Saved Messages. The mechanism is documented in ARCHITECTURE.md §8.2.
TTL_GUARD = _optional_bool("TTL_GUARD", True)

# In addition to sending it to Saved Messages, keep a copy on disk under
# data/ttl_media/. Off by default, because that directory has no size ceiling.
TTL_GUARD_LOCAL_COPY = _optional_bool("TTL_GUARD_LOCAL_COPY", False)

# Attach the raw media structure to the failure report and to the log. The
# guard cannot reproduce a failure on demand — the media is gone by the time
# anyone looks — so when something does not get saved, this is what makes the
# next occurrence diagnosable instead of guessable. Off by default: the dump
# contains file ids and access hashes, and it is long.
TTL_GUARD_DEBUG = _optional_bool("TTL_GUARD_DEBUG", False)

# How long a local copy stays on disk, in days. The directory used to have no
# ceiling at all: every rescued photo and video note stayed forever, so on a
# small server the feature slowly filled the disk. Old files are removed
# whenever a new one is written (see ttl_guard._save_local), which is exactly
# when the directory can grow. The floor of 1 day is deliberate — a value of 0
# would delete each file the moment the next one arrived.
TTL_MEDIA_RETENTION_DAYS = _optional_int("TTL_MEDIA_RETENTION_DAYS", 2, minimum=1)

# --- Display timezone ---
# Every time shown in a Telegram message goes through this zone. Without it the
# times came from the container's clock, which is UTC — so a message sent at
# 21:30 in Tehran was reported as 18:00, and the report looked broken even
# though the underlying timestamps were correct. Naming the zone in code rather
# than relying on the container's TZ keeps the output right when the bot is run
# straight from a shell too.
DISPLAY_TIMEZONE = os.getenv("DISPLAY_TIMEZONE", "").strip() or "Asia/Tehran"

# --- Anti-delete media cache ---
# The deletion report re-sends a deleted photo or voice from its stored file
# id, which costs nothing on arrival. But a file reference has a limited life
# and the message it belongs to is gone by then, so it cannot be refreshed —
# when it has expired the media is unreachable. Keeping a copy of *small* media
# as it arrives closes that hole, at the price of a download per media message.
DELETE_CACHE_MEDIA = _optional_bool("DELETE_CACHE_MEDIA", True)

# Only media at or below this size is copied (megabytes). The cap is the whole
# point: a photo or voice is worth a background download on every private
# message, a 300 MB video is not.
DELETE_CACHE_MAX_MB = _optional_int("DELETE_CACHE_MAX_MB", 5, minimum=1, maximum=100)

# Hard ceiling for the whole cache directory (megabytes). Age alone does not
# bound it — a busy day of photo-sharing would still fill a small disk — so the
# oldest files are dropped once the total passes this.
DELETE_CACHE_TOTAL_MB = _optional_int("DELETE_CACHE_TOTAL_MB", 500, minimum=10)

# --- Automatic enforcement of the global lists (.gban / .gmute) ---
# .gban restricts a user in the groups they are a member of *at that moment*.
# This option is the second layer: when a listed user later posts in one of
# the managed groups, the action is applied there too. Without it, "global"
# only means "at the time it ran" and the user is back by rejoining.
#
# On by default, because without it half the meaning of gban is lost — but,
# like TTL_GUARD, it is one environment variable away from being off.
GBAN_ENFORCE = _optional_bool("GBAN_ENFORCE", True)

# Delay between requests during a global operation (seconds). Each group is a
# separate request, and with no delay a command spanning a few dozen groups
# quickly hits FloodWait — which, on a real account, means the whole account
# gets limited, not just this one command failing. The floor of 50 ms keeps
# setting it to zero from removing the protection.
GLOBAL_ACTION_DELAY = _optional_int(
    "GLOBAL_ACTION_DELAY_MS", 350, minimum=50, maximum=10_000
) / 1000

# --- AI engine settings ---


def _key_list() -> list:
    """
    Read the Google API keys from the environment (comma-separated).

    Supporting several keys is deliberate: when one key's daily quota runs
    out, the engine rotates to the next one (utils/ai_engines.py).

    Only AI_API_KEYS is read, never the older AI_API_KEY: in existing .env
    files that variable still holds the previous (OpenAI-compatible) service's
    key, and reading it means sending an invalid key to Google and taking the
    entire AI layer offline.

    There is no default key in the source and there must not be: a key in the
    code means everyone who clones the repo runs on someone else's quota, and
    the moment the repo goes public that key is burned.

    An empty list is not an error — the bot starts and only the AI commands
    refuse with a clear message (utils/ai_engines.py::_keys). Everything else
    (logger, weather, prices, media guard) needs no key.
    """
    raw = os.getenv("AI_API_KEYS", "")
    return [k.strip() for k in raw.replace("\n", ",").split(",") if k.strip()]


AI_API_KEYS = _key_list()

if not AI_API_KEYS:
    log.warning(
        "AI_API_KEYS is not set — AI commands will refuse to run. "
        "Get a key from https://aistudio.google.com/apikey and put it in .env."
    )
    if os.getenv("AI_API_KEY", "").strip():
        # A trap hit in practice: an old .env keeps the *previous* service's
        # key under the singular name and the user believes they have a key.
        log.warning(
            "found the legacy AI_API_KEY (singular). It belongs to the old, "
            "removed gateway and is ignored; rename it to AI_API_KEYS only if "
            "it really is a Google AI Studio key."
        )

# Google model prefixes plus the aliases that ai_engines.MODELS understands.
# (These few strings are duplicated on purpose so config does not depend on
#  ai_engines; ai_engines imports config, and the dependency would be mutual.)
_GOOGLE_MODEL_PREFIXES = ("gemini", "gemma", "learnlm", "nano-banana", "imagen", "veo")
_MODEL_ALIASES = ("flash", "flash37", "lite", "pro", "image", "tts")


def _model_name(env_name: str, default: str) -> str:
    """
    Read a model name from the environment, checking it is a Google one.

    The previous engine was an OpenAI-compatible service and existing .env
    files still carry things like `AI_MODEL=gpt-5-mini`. Sending that name to
    Gemini returns a 404 and disables the whole assistant with no visible
    cause, so an unrelated value is ignored and logged.
    """
    raw = os.getenv(env_name, "").strip()
    if not raw:
        return default

    lowered = raw.lower()
    if lowered.startswith(_GOOGLE_MODEL_PREFIXES) or lowered in _MODEL_ALIASES:
        return raw

    log.warning(
        "%s=%r is not a Google model name (probably left over from the old "
        "service); using %s instead",
        env_name, raw, default,
    )
    return default


# Default conversation model.
AI_MODEL = _model_name("AI_MODEL", "gemini-3.6-flash")

# Image generation model (the .img command).
# Note: on free-plan keys these models have a quota of zero and return 429;
# the command works, but it needs a key on a paid plan.
AI_IMAGE_MODEL = _model_name("AI_IMAGE_MODEL", "gemini-3.1-flash-image")

# Default text-to-speech model and voice (the .tts command).
AI_TTS_MODEL = _model_name("AI_TTS_MODEL", "gemini-3.1-flash-tts-preview")
AI_TTS_VOICE = os.getenv("AI_TTS_VOICE", "Kore").strip() or "Kore"

# Maximum wait for each chunk of the response (seconds).
AI_TIMEOUT = _optional_int("AI_TIMEOUT", 120, minimum=10, maximum=900)

# How many messages are kept per chat as conversation memory.
AI_MAX_TURNS = _optional_int("AI_MAX_TURNS", 12, minimum=2, maximum=100)

# Maximum number of concurrent requests to the AI service.
# The floor of 1 is critical: zero means the semaphore never opens.
AI_MAX_CONCURRENCY = _optional_int("AI_MAX_CONCURRENCY", 5, minimum=1, maximum=50)

# Size ceiling for a file downloaded from Telegram and sent to the model (MB).
# Files up to 14 MB go inline in the request; anything larger is uploaded
# through the Files API. This overall cap keeps a several-hundred-megabyte
# video from filling up memory.
AI_MEDIA_MAX_MB = _optional_int("AI_MEDIA_MAX_MB", 25, minimum=1, maximum=2000)
AI_MEDIA_MAX_BYTES = AI_MEDIA_MAX_MB * 1024 * 1024
