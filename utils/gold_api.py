"""
18-karat gold price from TGJU (page scrape — there is no official API).

Like the other utils modules it always returns a display-ready Persian string
and never raises into a handler. The raw exception text does not reach the
user's message either: it is English and meaningless to them, and any Markdown
characters in it would break the message rendering.
"""

import asyncio
import logging
import re
import time

from utils.net import HttpError, fetch_text

log = logging.getLogger(__name__)

GOLD_URL = "https://www.tgju.org/profile/geram18"

# The price sits inside a span carrying this column id. If the site's markup
# changes, the pattern stops matching and the "site structure changed" message
# is returned — which is exactly what that situation is.
_PRICE_PATTERN = re.compile(
    r'<span[^>]*data-col="info\.last_trade\.PDrCotVal"[^>]*>([\d,]+)</span>'
)

# Plausible range for the per-gram price in rial. It catches the case where
# the pattern matches some unrelated number on the page; showing an obviously
# wrong price is worse than showing an error.
_MIN_RIAL = 1_000_000
_MAX_RIAL = 10_000_000_000

ERROR_NETWORK = "❌ دریافت قیمت طلا ممکن نشد؛ کمی بعد دوباره تلاش کن."
ERROR_SCHEMA = "❌ ساختار سایت مرجع تغییر کرده و دریافت قیمت امکان‌پذیر نیست."

# ---------------------------------------------------------------------------
# RAM cache
# ---------------------------------------------------------------------------
# There is no API here, only a page scrape, and the page is ~1.1 MB. Without a
# cache every .gold pulled that much down; three calls in a row were enough for
# TGJU to start timing the requests out. The gram price does not move fast
# enough for 60 s of staleness to matter.
#
# Same two rules as currency_api's cache, for the same reasons: the timestamp is
# time.monotonic() so an NTP jump cannot pin the cache permanently valid or
# permanently expired, and only a *successful* result is stored — caching the
# error string would keep serving it for a full TTL after the site recovered.
CACHE_TTL = 60

_cache_text: str | None = None
_cache_at: float = 0.0

# Single-flight lock: concurrent .gold commands on a cold cache produce exactly
# one request instead of one download each.
_fetch_lock = asyncio.Lock()


def _read_cache() -> str | None:
    """Return the cached price block if it is still within the TTL."""
    if _cache_text is not None and (time.monotonic() - _cache_at) < CACHE_TTL:
        log.debug("gold price served from cache")
        return _cache_text
    return None


def _build_message(page: str) -> str | None:
    """
    Turn the scraped page into the display block.

    Returns None when the page did not yield a usable price; the caller maps
    that to ERROR_SCHEMA.
    """
    match = _PRICE_PATTERN.search(page)
    if not match:
        log.warning("gold: price pattern not found in TGJU page (%d bytes)", len(page))
        return None

    try:
        rial = int(match.group(1).replace(",", ""))
    except ValueError:
        log.warning("gold: matched value is not a number: %r", match.group(1))
        return None

    if not _MIN_RIAL <= rial <= _MAX_RIAL:
        log.warning("gold: price %d rial is outside the plausible range", rial)
        return None

    # The source quotes in rial; dividing by 10 gives Toman.
    gram_toman = rial // 10
    milli_toman = gram_toman // 1000

    return (
        "🪙 **قیمت طلای ۱۸ عیار:**\n\n"
        f"⚖️ هر گرم: {gram_toman:,} تومان\n"
        f"🤏 هر میلی‌گرم: {milli_toman:,} تومان"
    )


async def get_milli_gold_price(force_refresh: bool = False) -> str:
    """
    Price per gram and per milligram of 18-karat gold, in Toman.

    The result is a display-ready string and this never raises;
    force_refresh=True bypasses the cache.
    """
    global _cache_text, _cache_at

    if not force_refresh:
        cached = _read_cache()
        if cached is not None:
            return cached

    async with _fetch_lock:
        # Re-check: while we waited on the lock another call may have refreshed.
        if not force_refresh:
            cached = _read_cache()
            if cached is not None:
                return cached

        try:
            page = await fetch_text(GOLD_URL)
        except HttpError as e:
            log.warning("gold: fetching TGJU failed — %s", e)
            return ERROR_NETWORK

        text = _build_message(page)
        if text is None:
            return ERROR_SCHEMA

        _cache_text = text
        _cache_at = time.monotonic()
        return text
