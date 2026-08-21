"""
Runtime patches for the pinned MTProto library.

The library version is pinned in requirements.txt because its behaviour breaks
between releases, so bugs that come from the library are patched here, at
runtime, rather than by upgrading. Patch 1 has since been fixed upstream and
now skips itself; patches 2 and 3 are still load-bearing.

Rules shared by every patch:
  * They are idempotent; calling them repeatedly is harmless.
  * They must be applied *before* Client() is constructed.
  * If the library's structure has changed they return False and log a
    warning rather than crashing; the userbot must not fail to start over a
    patch.
"""

import logging
import time

log = logging.getLogger(__name__)


# ==========================================================
# 1. Valid peer-id ranges
# ==========================================================
#
# Telegram has expanded the id space for channels and groups, and newer ids
# exceed Pyrogram's old ceiling (2^31). The range table in pyrogram/utils.py
# is hardcoded:
#
#     MIN_CHANNEL_ID = -1002147483647   <- ceiling too low
#     MAX_CHANNEL_ID = -1000000000000
#     MIN_CHAT_ID    = -2147483647      <- ceiling too low
#
# and get_peer_type() raises "ValueError: Peer id invalid" for any id outside
# those ranges. That error is not merely log noise: for a channel message
# whose peer is not already stored in the session, client.handle_updates calls
# resolve_peer(get_channel_id(...)), and only ChannelPrivate is caught there;
# the ValueError escapes the background task and the update is dropped
# entirely. Messages from newer channels never reach the handlers at all.
#
# The new values come from TDLib's DialogId ranges (every chat kind is below
# 10^12) and line up with Pyrogram's own MAX_USER_ID (999999999999):
#
#     basic group : [-999999999999   , -1]
#     channel     : [-1999999999999  , -1000000000001]
#
# The two intervals stay disjoint and the check order in get_peer_type is
# untouched, so previously valid ids resolve exactly as before.
NEW_MIN_CHAT_ID = -999_999_999_999
NEW_MIN_CHANNEL_ID = -1_999_999_999_999


def patch_peer_id_ranges() -> bool:
    """Widen the group/channel id ranges. True means the patch was applied."""
    try:
        from pyrogram import utils as pyrogram_utils
    except ImportError:  # pragma: no cover - only if pyrogram is not installed
        log.warning("peer-id range patch skipped: pyrogram could not be imported")
        return False

    try:
        old_channel = pyrogram_utils.MIN_CHANNEL_ID
        old_chat = pyrogram_utils.MIN_CHAT_ID
    except AttributeError:
        # Expected on the current library: it classifies peers without a range
        # table, so there is nothing left to widen. INFO rather than WARNING —
        # this is the patch becoming unnecessary, not a failure.
        log.info("peer-id range patch not needed: this version has no range table")
        return False

    changed = False

    # Only ever widen; if a future version ships a broader range, leave it be.
    if old_channel > NEW_MIN_CHANNEL_ID:
        pyrogram_utils.MIN_CHANNEL_ID = NEW_MIN_CHANNEL_ID
        changed = True

    if old_chat > NEW_MIN_CHAT_ID:
        pyrogram_utils.MIN_CHAT_ID = NEW_MIN_CHAT_ID
        changed = True

    if changed:
        log.info(
            "widened peer-id ranges (channel: %s -> %s, basic group: %s -> %s)",
            old_channel, pyrogram_utils.MIN_CHANNEL_ID,
            old_chat, pyrogram_utils.MIN_CHAT_ID,
        )

    return changed


# ==========================================================
# 2. Clock offset for msg_id generation
# ==========================================================
#
# In MTProto msg_id is derived directly from the client clock, and a clock
# that runs behind earns a BadMsgNotification[16] ("msg_id is too low") from
# the server, which effectively stops the session working.
#
# The library has no time-correction mechanism at all
# (session/internals/msg_id.py reads time.time() directly), and inside a
# container the clock cannot be set because it belongs to the host kernel. So
# rather than touching the system clock, only msg_id generation is shifted:
# the rest of the process still sees the system clock.
#
# A naming note: MsgId already has an attribute called offset, which is the
# intra-second counter. Ours is deliberately named time_offset to avoid a
# collision.

_time_patch_applied = False


class _ShiftedClock:
    """
    Stand-in for the `time` module inside Pyrogram's msg_id module.

    The correction used to be a line-for-line copy of upstream's
    `MsgId.__new__` with the offset added, which meant it silently depended on
    that function's internals: 2.0.106 keeps `offset`/`last_time` class
    attributes, while the current library computes msg_id from a single
    `_last_msg_id`. Running the old copy against the new class raised
    AttributeError on *every* outgoing message — a total outage from a patch
    whose only job was to add a number.

    Shifting the *clock that module reads* instead of reimplementing the
    algorithm removes that coupling: both versions derive msg_id from
    `time.time()` in this one module, and nothing else in the process sees a
    modified clock. Every other attribute is delegated, so the module keeps
    working if it ever reaches for something besides time().
    """

    def __init__(self, msg_id_class):
        self._msg_id_class = msg_id_class

    def __getattr__(self, name):
        return getattr(time, name)

    def time(self) -> float:
        return time.time() + getattr(self._msg_id_class, "time_offset", 0.0)


def apply_time_offset(offset: float) -> bool:
    """
    Record how far the system clock is from real time (in seconds).

    The offset is *absolute*, not cumulative: each new measurement calls this
    function with the new value. That is because every measurement is made
    against raw time.time(), so its result already means "system vs. truth",
    and accumulating offsets would introduce error.
    """
    global _time_patch_applied

    try:
        from pyrogram.session.internals import msg_id as msg_id_module
        from pyrogram.session.internals.msg_id import MsgId
    except ImportError:  # pragma: no cover
        log.warning("clock correction skipped: MsgId could not be imported")
        return False

    # Set before the shim is installed, so the first msg_id already reads it.
    MsgId.time_offset = float(offset)

    if not _time_patch_applied:
        # The module's own `time` reference is replaced rather than the class's
        # method: msg_factory.py and session.py bind to the MsgId class object
        # at import time, but they all reach the clock through this module.
        msg_id_module.time = _ShiftedClock(MsgId)
        _time_patch_applied = True

    return True


# One asymmetry worth knowing about: the library keeps a monotonic watermark
# (`_last_msg_id`) and never emits a msg_id below it, so *raising* the offset
# takes effect immediately while *lowering* it only takes effect once real time
# passes the highest id already sent. In the case this reversal exists for —
# the host's own clock being fixed — that is a non-event, because the system
# clock jumps forward by the very amount the offset is dropping. It is not
# worked around: forcing the watermark down would emit a msg_id lower than one
# already sent, which is precisely the BadMsgNotification[16] this whole patch
# exists to prevent.


def get_time_offset() -> float:
    """The current clock offset (0 means no correction has been applied)."""
    try:
        from pyrogram.session.internals.msg_id import MsgId
    except ImportError:  # pragma: no cover
        return 0.0

    return float(getattr(MsgId, "time_offset", 0.0))


# ==========================================================
# 3. Photos that carry no downloadable size
# ==========================================================
#
# types.Photo._parse keeps only PhotoSize and PhotoSizeProgressive entries and
# then takes the largest one with
#
#     main = photos[-1]
#
# so a photo carrying neither — one that arrives with just a stripped preview,
# for instance — makes that an IndexError.
#
# The failure does not stay inside the photo. Dispatcher.handler_worker parses
# every update *before* running any handler, and that parse sits inside the
# same try block as the handler loop (pyrogram/dispatcher.py:205-260), so one
# unparsable photo drops the entire update: the TTL guard, the anti-delete
# logger and every command miss it, and the only trace is a library-level
# traceback with no project frames. Web-page previews reach the same code path
# (web_page.py:140), so this is not limited to timed media.
#
# The patch turns the crash into "no photo object", which is what the rest of
# Pyrogram already handles — Photo._parse returns None for a PhotoEmpty. The
# TTL guard builds its file id from the raw media anyway, so it can still
# rescue a photo the high-level parser gave up on.


def patch_tolerant_photo_parse() -> bool:
    """Stop an undescribable photo from dropping the whole update."""
    try:
        from pyrogram import types as pyrogram_types
    except ImportError:  # pragma: no cover - only if pyrogram is not installed
        log.warning("photo-parse patch skipped: pyrogram could not be imported")
        return False

    photo_cls = getattr(pyrogram_types, "Photo", None)
    original = getattr(photo_cls, "_parse", None)

    if original is None:
        log.warning("photo-parse patch skipped: types.Photo._parse is gone")
        return False

    if getattr(original, "_tolerant", False):
        return False  # already applied

    def _tolerant_parse(client, photo, ttl_seconds=None):
        try:
            return original(client, photo, ttl_seconds)
        except IndexError:
            log.warning("photo with no downloadable size; treated as no media")
            return None

    _tolerant_parse._tolerant = True
    photo_cls._parse = staticmethod(_tolerant_parse)

    return True
