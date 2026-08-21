"""
Live crypto market prices, fetched and formatted, with a two-exchange design.

Nobitex is not reachable from this host (its domain no longer resolves), so
the primary source is now Wallex, with Ramzinex added as a fallback. If Wallex
fails for any reason the same request is served from Ramzinex automatically
and the user never notices the source changed.

This module follows the utils contract: it always returns a display-ready
Persian string and never raises into a handler, so a network outage can never
kill a handler inside Pyrogram's dispatcher.
"""

import asyncio
import logging
import time
from typing import NamedTuple

import aiohttp

log = logging.getLogger(__name__)

WALLEX_MARKETS_URL = "https://api.wallex.ir/v1/markets"
RAMZINEX_PAIRS_URL = "https://publicapi.ramzinex.com/exchange/api/v1.0/exchange/pairs"

# Cache lifetime (seconds). Its main purpose is removing the network round
# trip for back-to-back commands; on top of that these two APIs return a few
# hundred kilobytes each, and the cache makes that cost rare in practice.
CACHE_TTL = 60

# Network timeouts: sock_connect is short so a dead network is detected fast
# and the user is not left waiting behind an interactive command. Since two
# exchanges may be tried in sequence, these apply *per request*.
_TIMEOUT = aiohttp.ClientTimeout(total=20, sock_connect=8, sock_read=12)

# The only error message this module emits (when every exchange has failed).
ERROR_TEXT = "❌ خطا در دریافت اطلاعات از صرافی‌ها"

# (Persian label, currency code, emoji) — all three are fixed, controlled values.
_MARKETS = (
    ("تتر", "usdt", "💵"),
    ("بیت‌کوین", "btc", "₿"),
    ("اتریوم", "eth", "Ξ"),
)

# Currency units that appear in the API responses. Telling rial from Toman is
# critical: confusing the two means a price that is off by exactly 10x.
_RIAL_UNITS = frozenset({"irr", "rls", "rial"})
_TOMAN_UNITS = frozenset({"irt", "tmn", "toman"})


class Quote(NamedTuple):
    """
    A normalised quote for one currency.

    Both parsers (Wallex and Ramzinex) reduce their output to this shape, so
    nothing downstream knows which exchange served the data.
    """

    toman: float  # always Toman, never rial
    change: float | None  # 24-hour change in percent; None means unknown


# ---------------------------------------------------------------------------
# RAM cache
# ---------------------------------------------------------------------------
# Only a successful result is kept here. The timestamp uses monotonic rather
# than time.time, because a system clock jump (an NTP sync, say) can leave the
# cache permanently valid or permanently expired.
_cache_text: str | None = None
_cache_at: float = 0.0

# Single-flight lock: when several commands run concurrently on a cold cache,
# exactly one of them goes to the network and the rest get its result.
_fetch_lock = asyncio.Lock()


def _read_cache() -> str | None:
    """Return the cached result if it is still within the TTL."""
    if _cache_text is not None and (time.monotonic() - _cache_at) < CACHE_TTL:
        log.debug("market prices served from cache")
        return _cache_text
    return None


async def get_market_prices(force_refresh: bool = False) -> str:
    """
    Return the live USDT/BTC/ETH prices in Toman.

    The result is a display-ready string; if *every* exchange fails,
    ERROR_TEXT is returned. force_refresh=True bypasses the cache.
    """
    global _cache_text, _cache_at

    if not force_refresh:
        cached = _read_cache()
        if cached is not None:
            return cached

    async with _fetch_lock:
        # Re-check the cache: while we were waiting on the lock another
        # request may have refreshed the result already.
        if not force_refresh:
            cached = _read_cache()
            if cached is not None:
                return cached

        try:
            quotes, source = await _fetch_quotes()
            text = _build_message(quotes)
        except Exception:
            log.exception("market prices unavailable from every exchange")
            return ERROR_TEXT

        log.info("market prices built from %s", source)

        # Only a successful result is cached. Caching the error message would
        # keep returning it for a full 60 s even if the network came back a
        # second later.
        _cache_text = text
        _cache_at = time.monotonic()
        return text


# ---------------------------------------------------------------------------
# Network layer and failover logic
# ---------------------------------------------------------------------------


async def _fetch_quotes() -> tuple[dict[str, Quote], str]:
    """
    Fetch the prices from the first healthy exchange.

    Exchanges are tried in _SOURCES order: Wallex first (primary), then
    Ramzinex (fallback) if that failed. The catch is deliberately broad and
    does not stop at network errors (timeout, DNS, 5xx); malformed JSON and
    schema drift also switch to the next exchange, because from the user's
    side both mean the same thing: "this exchange is not usable right now".

    An exception is raised only when every source fails, so the caller can
    turn it into ERROR_TEXT.
    """
    errors: list[str] = []

    # One shared session for both attempts; like the other utils modules it is
    # created and closed within this call, so it needs no hook in main.py.
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        for index, (name, url, parse) in enumerate(_SOURCES):
            try:
                log.info("requesting market prices from %s", name)
                async with session.get(url) as resp:
                    resp.raise_for_status()
                    # content_type=None because these exchanges do not always
                    # send a clean application/json header.
                    payload = await resp.json(content_type=None)

                quotes = parse(payload)
            except Exception as exc:
                # Swallow the error and move to the next exchange, but always
                # log it, so a geo-block or outage at Wallex shows up in the
                # logs rather than as a silent source switch.
                log.warning("exchange %s failed (%s: %s)", name, type(exc).__name__, exc)
                errors.append(f"{name}: {type(exc).__name__}")
                continue

            if index > 0:
                log.warning("failed over to backup exchange %s", name)
            return quotes, name

    raise RuntimeError("every exchange failed — " + " | ".join(errors))


# ---------------------------------------------------------------------------
# Parsers — each turns one exchange's raw shape into dict[str, Quote]
# ---------------------------------------------------------------------------


def _parse_wallex(payload: dict) -> dict[str, Quote]:
    """
    Parse the Wallex response.

    Expected shape:
        {"success": true,
         "result": {"symbols": {"USDTTMN": {"quoteAsset": "TMN",
                                            "stats": {"lastPrice": "189200.00",
                                                      "24h_ch": 0.59}}}}}

    On units: the market key ends in TMN and the price is already in *Toman*,
    so unlike Nobitex no division by 10 is needed.
    """
    if not isinstance(payload, dict):
        raise ValueError("Wallex response is not an object")

    # Wallex reports a logical failure as success=false under a 200 status.
    if payload.get("success") is False:
        raise ValueError(f"Wallex reported failure: {payload.get('message')}")

    symbols = (payload.get("result") or {}).get("symbols")
    if not isinstance(symbols, dict):
        raise ValueError("Wallex response has no result.symbols")

    quotes: dict[str, Quote] = {}

    for _, code, _ in _MARKETS:
        key = f"{code.upper()}TMN"  # e.g. usdt -> USDTTMN
        entry = symbols.get(key)
        if not isinstance(entry, dict):
            raise ValueError(f"Wallex response has no symbol {key}")

        # The unit is read from the response, never guessed; if Wallex ever
        # switches this key to rial, failing is better than a 10x price.
        unit = str(entry.get("quoteAsset", "")).strip().lower()
        if unit not in _TOMAN_UNITS:
            raise ValueError(f"unexpected quote unit for {key}: {unit!r}")

        stats = entry.get("stats")
        if not isinstance(stats, dict):
            raise ValueError(f"Wallex symbol {key} has no stats")

        price = _to_number(stats.get("lastPrice"))
        if price is None or price <= 0:
            raise ValueError(f"invalid Wallex price for {key}: {stats.get('lastPrice')!r}")

        quotes[code] = Quote(price, _to_number(stats.get("24h_ch")))

    return quotes


def _parse_ramzinex(payload: dict) -> dict[str, Quote]:
    """
    Parse the Ramzinex response (the fallback exchange).

    Expected shape:
        {"status": 0,
         "data": [{"base_currency_symbol":  {"en": "btc", "fa": "بیت کوین"},
                   "quote_currency_symbol": {"en": "irr", "fa": "ریال"},
                   "financial": {"last24h": {"close": 122749999991.81,
                                             "change_percent": 1.24}}}]}

    Two differences from Wallex that matter, both found by inspecting the real
    response while writing this code:

    1) The symbols are not plain strings but multilingual dicts, so the "en"
       key has to be pulled out of them.
    2) The price unit is **rial** (quote = irr), not Toman, so a division by
       10 is required. This was confirmed by cross-checking all three coins
       against Wallex (under 0.3% apart after dividing, 10x apart without it).
    """
    if not isinstance(payload, dict):
        raise ValueError("Ramzinex response is not an object")

    pairs = payload.get("data")
    if not isinstance(pairs, list):
        raise ValueError("Ramzinex response has no data list")

    wanted = {code for _, code, _ in _MARKETS}
    quotes: dict[str, Quote] = {}

    for pair in pairs:
        if not isinstance(pair, dict):
            continue

        base = _symbol_en(pair.get("base_currency_symbol"))
        # The first valid pair wins; later ones are ignored.
        if base not in wanted or base in quotes:
            continue

        # Delisted or temporarily suspended pairs carry no valid price.
        if pair.get("is_delist") or pair.get("is_temporarily_suspended"):
            continue

        last24h = (pair.get("financial") or {}).get("last24h")
        if not isinstance(last24h, dict):
            continue

        price = _to_number(last24h.get("close"))
        if price is None or price <= 0:
            continue

        # Convert based on what the API declares, not on our assumption.
        unit = _symbol_en(pair.get("quote_currency_symbol"))
        if unit in _RIAL_UNITS:
            price /= 10
        elif unit not in _TOMAN_UNITS:
            continue  # a non-Iranian pair (btc/usdt, say) is not our concern

        quotes[base] = Quote(price, _to_number(last24h.get("change_percent")))

    missing = sorted(wanted - set(quotes))
    if missing:
        raise ValueError(f"Ramzinex response is missing pairs: {missing}")

    return quotes


# Order matters: the first entry is the primary source, the rest fallbacks.
_SOURCES = (
    ("Wallex", WALLEX_MARKETS_URL, _parse_wallex),
    ("Ramzinex", RAMZINEX_PAIRS_URL, _parse_ramzinex),
)


# ---------------------------------------------------------------------------
# Output rendering
# ---------------------------------------------------------------------------


def _build_message(quotes: dict[str, Quote]) -> str:
    """
    Build the final text from the normalised quotes.

    No API-derived value is ever placed inside a Markdown construct; the only
    `**` present wraps the fixed header, so the output can never break
    Telegram's parser.
    """
    lines = ["📊 **وضعیت لحظه‌ای بازار**", ""]

    for label, code, icon in _MARKETS:
        quote = quotes.get(code)
        if quote is None:
            raise ValueError(f"normalised data has no quote for {code}")

        change = _format_change(quote.change)
        # rstrip covers the case where the change is unknown and the helper
        # returns an empty string.
        lines.append(f"{icon} {label}: {round(quote.toman):,} تومان {change}".rstrip())

    return "\n".join(lines)


def _symbol_en(raw) -> str:
    """
    Pull the English symbol out of Ramzinex's multilingual structure.

    A plain string is accepted too, so the code survives the API ever
    un-nesting these values.
    """
    if isinstance(raw, dict):
        raw = raw.get("en")
    return str(raw or "").strip().lower()


def _to_number(raw) -> float | None:
    """
    Safely convert an API value to a number.

    Wallex sends the price as a string ("189200.0000") and Ramzinex as a raw
    number (122749999991.81), so this has to accept both.
    """
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return float(str(raw).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _format_change(change: float | None) -> str:
    """
    Format the 24-hour change percentage.

    The output is built only from digits and safe characters, so it can never
    break Telegram's Markdown parsing.
    """
    if change is None:
        return ""
    if change > 0:
        return f"(+{change:.2f}% 🟢)"
    if change < 0:
        # The number formatting adds the minus sign itself.
        return f"({change:.2f}% 🔴)"
    return f"({change:.2f}% ⚪)"
