"""
Logging configuration and throttling of known noise.

The philosophy here: nothing is ever *discarded*, only its *repetition* is
throttled.

A userbot runs on a real account and usually over an unstable network, so a
few classes of error are unavoidable: sockets dropping and reconnecting,
request timeouts, and updates from chats we no longer have access to. Pyrogram
handles and retries all of these itself; what is left is a wall of log output
that buries the real entries underneath it.

The approach: each known noise pattern is shown *once* per time window, and
the suppressed occurrences are counted on that same line. So a genuine outage
is still visible — as one summary line instead of a hundred repeats.

Language: every string this module produces is **English**. Logs are for
developers and bug reports, they have to stay legible in a terminal with no
right-to-left support, and pasting one into an issue must not reorder it.
"""

import asyncio
import logging
import os
import time
from typing import NamedTuple

log = logging.getLogger(__name__)

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
LOG_DATEFMT = "%H:%M:%S"

DEFAULT_LEVEL = logging.INFO

# Dedicated logger for Pyrogram background-task errors (explained further down).
BACKGROUND_LOGGER = "userbot.background"


class NoiseRule(NamedTuple):
    """
    One known noise pattern.

    logger : logger name (child loggers are included)
    needle : a fragment of the message text; an empty string matches every
             message from that logger
    window : interval between two displays, in seconds
    level  : the level the record is shown at
    note   : an explanation appended to the log line, to make it actionable

    A maintenance note: needle has to match the *actual* text of the target
    log call. For the project's own rules (auto_seen, group_admin and
    bot_cleaner) that means changing the message means changing it here too —
    the boot test does not catch this.
    """
    logger: str
    needle: str
    window: float
    level: int
    note: str


NOISE_RULES = (
    # --- Group 1: network ---
    # These are symptoms, not causes: Pyrogram retries the connection three
    # times and rebuilds the session itself. Each outage produces dozens of
    # lines.
    NoiseRule(
        "asyncio", "socket.send() raised exception", 300.0, logging.INFO,
        "Telegram socket dropped; Pyrogram reconnects on its own",
    ),
    NoiseRule(
        "pyrogram.connection.connection", "Unable to connect due to network issues",
        300.0, logging.WARNING,
        "cannot reach the Telegram data centre; retries continue",
    ),
    NoiseRule(
        "pyrogram.session.session", "Retrying", 300.0, logging.INFO,
        "request retried after a timeout or a transient server error",
    ),
    NoiseRule(
        "pyrogram.session.session", "Server sent transport error", 300.0, logging.WARNING,
        "server refused the connection; the session is being rebuilt",
    ),

    # --- Group 2: the clock ---
    # This one is not noise and has to be seen, but Pyrogram's raw text does
    # not say what the user should do about it; the note makes up for that.
    NoiseRule(
        "pyrogram.session.session", "BadMsgNotification", 120.0, logging.WARNING,
        "system clock is out of sync with Telegram — keep CLOCK_SYNC enabled "
        "or fix the host clock with NTP",
    ),

    # --- Group 3: updates that cannot be processed at all ---
    # These errors happen inside Pyrogram's own dispatcher and never reach our
    # handlers (or @safe_handler). For instance, when a message is edited that
    # replies to a message in a channel we have been removed from, Pyrogram
    # calls channels.GetMessages to build the full object and gets
    # CHANNEL_PRIVATE. There is nothing we can do, and that one update is
    # dropped.
    NoiseRule(
        "pyrogram.dispatcher", "CHANNEL_PRIVATE", 600.0, logging.INFO,
        "channel/supergroup is out of reach; that single update was skipped",
    ),
    NoiseRule(
        "pyrogram.dispatcher", "ID not found", 600.0, logging.INFO,
        "this chat is not stored in the session; that single update was skipped",
    ),
    NoiseRule(
        "pyrogram.dispatcher", "Peer id invalid", 600.0, logging.INFO,
        "chat id falls outside the known range",
    ),

    # --- Group 4: handlers that run on every message ---
    # None of these handlers carries @safe_handler (its error message would go
    # to the other party's chat), so a log line is their only output — and without
    # throttling they produce one per *message*. The needle has to stay in
    # sync with the log text in those modules.
    NoiseRule(
        "plugins.auto_seen", "auto-seen failed", 300.0, logging.WARNING,
        "remove this chat from the auto-seen list with .delseen",
    ),
    NoiseRule(
        "plugins.group_admin", "global-list enforcement failed", 300.0, logging.WARNING,
        "check whether the account still has ban rights in that group",
    ),
    NoiseRule(
        "plugins.bot_cleaner", "bot-clean failed", 300.0, logging.WARNING,
        "the message was left alone; turn the cleaner off with .botclean off if this persists",
    ),

    # --- Group 5: background-task errors ---
    # These are all logged from one place (install_asyncio_exception_handler),
    # so a single rule with an empty needle is enough.
    NoiseRule(BACKGROUND_LOGGER, "", 600.0, logging.WARNING, ""),
)


class _NoiseThrottleFilter(logging.Filter):
    """
    A handler-level filter that thins out repeated noise.

    It is installed on the handler and not on a logger, because a logger's
    filters only apply to that logger's own records — records from child
    loggers (pyrogram.session.session, say) would bypass it.
    """

    def __init__(self, rules=NOISE_RULES):
        super().__init__()
        self._rules = tuple(rules)
        # Both dicts are keyed by the rule's index.
        self._last_shown = {}
        self._suppressed = {}

    def filter(self, record: logging.LogRecord) -> bool:
        for index, rule in enumerate(self._rules):
            if not self._matches_logger(record.name, rule.logger):
                continue
            # getMessage is only called for records whose logger has already
            # matched, so its cost stays off the hot path.
            if rule.needle and rule.needle not in self._safe_message(record):
                continue

            return self._throttle(index, rule, record)

        return True

    @staticmethod
    def _matches_logger(record_name: str, rule_logger: str) -> bool:
        return record_name == rule_logger or record_name.startswith(rule_logger + ".")

    @staticmethod
    def _safe_message(record: logging.LogRecord) -> str:
        """
        The record's text, with no way for the filter to blow up.

        getMessage performs a real %-format and raises TypeError when the
        argument count does not match the placeholders. We are inside the
        logging path here; an exception at this point would lose the very
        error we were trying to see, so fall back to the raw text.
        """
        try:
            return record.getMessage()
        except Exception:
            return str(record.msg)

    def _throttle(self, index: int, rule: NoiseRule, record: logging.LogRecord) -> bool:
        # monotonic and not time: a project whose central problem is an
        # untrustworthy wall clock must not build its rate limiter on one.
        now = time.monotonic()
        last = self._last_shown.get(index)

        if last is not None and now - last < rule.window:
            self._suppressed[index] = self._suppressed.get(index, 0) + 1
            return False

        skipped = self._suppressed.pop(index, 0)
        self._last_shown[index] = now

        text = self._safe_message(record)
        if rule.note:
            text = f"{text}  <- {rule.note}"
        if skipped:
            minutes = max(1, int(rule.window // 60))
            text = f"{text}  [{skipped} similar hidden in the last {minutes} min]"

        record.msg = text
        record.args = None

        # The traceback is dropped because these failures are known and one
        # line says everything. exc_text is cleared too, since an earlier
        # formatter may have cached it.
        record.exc_info = None
        record.exc_text = None

        record.levelno = rule.level
        record.levelname = logging.getLevelName(rule.level)

        return True


def _resolve_level(level) -> int:
    """
    Resolve the log level from the argument, the LOG_LEVEL env var, or the
    default.

    Both names (DEBUG/INFO/…) and numbers are accepted. An invalid value does
    not raise — the default level stands, because a typo in .env must not keep
    the bot from starting.
    """
    if isinstance(level, int):
        return level

    raw = (level or os.getenv("LOG_LEVEL", "")).strip()
    if not raw:
        return DEFAULT_LEVEL

    if raw.isdigit():
        return int(raw)

    resolved = logging.getLevelName(raw.upper())
    if isinstance(resolved, int):
        return resolved

    # Logging is not configured yet, so this line deliberately uses print.
    print(f"[config] unknown LOG_LEVEL={raw!r}; falling back to INFO")
    return DEFAULT_LEVEL


def setup_logging(level=None) -> None:
    """
    Configure root logging and install the noise filter.

    Idempotent: calling it again does not add a duplicate filter.
    """
    resolved = _resolve_level(level)
    logging.basicConfig(level=resolved, format=LOG_FORMAT, datefmt=LOG_DATEFMT)
    logging.getLogger().setLevel(resolved)

    # Pyrogram is very chatty at INFO; only its warnings are shown. In DEBUG
    # that ceiling is lifted, since DEBUG is turned on deliberately and for
    # exactly those details.
    logging.getLogger("pyrogram").setLevel(
        logging.DEBUG if resolved <= logging.DEBUG else logging.WARNING
    )

    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, _NoiseThrottleFilter) for f in handler.filters):
            handler.addFilter(_NoiseThrottleFilter())


# ==========================================================
# Background-task errors
# ==========================================================
#
# Pyrogram dispatches every update through loop.create_task
# (Session.handle_packet). If an error occurs inside client.handle_updates
# that it does not catch itself, nobody reads that task's result and asyncio
# prints "Task exception was never retrieved" with a full traceback.
#
# These errors have nothing to do with our handlers and @safe_handler cannot
# catch them either. set_exception_handler — an official asyncio hook, not a
# monkeypatch — collapses the known cases into a single line and lets
# everything else reach the default handler exactly as before.

# Class names are matched instead of importing them directly, so this module
# stays decoupled from pyrogram.errors' structure (names move between
# versions).
_QUIET_ERROR_NAMES = frozenset({
    "PeerIdInvalid",
    "PeerIdNotSupported",
    "ChannelPrivate",
    "ChannelInvalid",
    "ChatForbidden",
    "UserDeactivated",
    "AuthKeyDuplicated",
})

# For KeyError and ValueError the message text decides; those types are so
# general that suppressing them wholesale would hide real bugs.
_QUIET_ERROR_MARKERS = (
    "ID not found",
    "Peer id invalid",
    "Peer type invalid",
)

_background_log = logging.getLogger(BACKGROUND_LOGGER)


def _is_known_background_noise(exc: BaseException) -> bool:
    # Since Python 3.11, ConnectionError and asyncio.TimeoutError are
    # subclasses of these two types.
    if isinstance(exc, (OSError, TimeoutError)):
        return True

    if type(exc).__name__ in _QUIET_ERROR_NAMES:
        return True

    text = str(exc)
    return any(marker in text for marker in _QUIET_ERROR_MARKERS)


def install_asyncio_exception_handler(loop: asyncio.AbstractEventLoop) -> None:
    """Collapse known background-task errors on the given loop."""

    def handler(event_loop: asyncio.AbstractEventLoop, context: dict) -> None:
        exc = context.get("exception")

        if exc is not None and _is_known_background_noise(exc):
            _background_log.warning(
                "known error in a Pyrogram background task: %s: %s",
                type(exc).__name__, exc,
            )
            return

        # Anything else is a real bug and has to be seen in full.
        event_loop.default_exception_handler(context)

    loop.set_exception_handler(handler)
