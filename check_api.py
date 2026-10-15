"""
Reachability check for every external API the bot depends on.

Run it on the server, from the project directory:

    python3 check_api.py

Each check sends the same request the bot itself sends (same URL, same query
parameters, same browser User-Agent as utils/net.py), so a pass here means the
command behind it can work from this host. The URLs are copied from the
modules named in each check; when one of those modules changes source, change
it here too.

Only the standard library is used, so the script runs with the system python3
on the host — no venv, no rebuilt container. The Gemini keys are read from the
environment first and then from the .env next to this file; they are never
printed, only their last four characters.

Exit code: 0 when every check passes, 1 when any fails (usable from cron).
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

TIMEOUT = 15

# Same as utils/net.py: TGJU answers a non-browser User-Agent with a 403.
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Same cap as utils/net.py; the TGJU page alone is ~1.1 MB.
MAX_BODY_BYTES = 4 * 1024 * 1024

# City used for the weather and prayer-time checks.
TEST_CITY = "Tehran"

GEMINI_ROOT = "https://generativelanguage.googleapis.com/v1beta"


class CheckFailed(Exception):
    """A check that reached a verdict of "not working", with the reason."""


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _get(url: str, headers: dict | None = None) -> bytes:
    """GET a URL and return the body; any failure becomes CheckFailed."""
    request_headers = {"User-Agent": BROWSER_UA}
    if headers:
        request_headers.update(headers)

    request = urllib.request.Request(url, headers=request_headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body = response.read(MAX_BODY_BYTES + 1)
    except urllib.error.HTTPError as e:
        raise CheckFailed(f"HTTP {e.code}{_error_detail(e)}") from None
    except urllib.error.URLError as e:
        # DNS failure, refused connection, TLS error. e.reason is the useful part.
        raise CheckFailed(f"network failure: {e.reason}") from None
    except (TimeoutError, OSError) as e:
        raise CheckFailed(f"network failure: {type(e).__name__}: {e}") from None

    if len(body) > MAX_BODY_BYTES:
        raise CheckFailed(f"response body larger than {MAX_BODY_BYTES} bytes")
    return body


def _error_detail(error: urllib.error.HTTPError) -> str:
    """Pull a short message out of an error body (Google puts it in JSON)."""
    try:
        payload = json.loads(error.read(64 * 1024))
        message = payload["error"]["message"]
    except Exception:
        return ""
    return f" — {message[:120]}"


def _get_json(url: str, headers: dict | None = None):
    body = _get(url, headers)
    try:
        return json.loads(body)
    except ValueError:
        raise CheckFailed("response is not valid JSON") from None


# ---------------------------------------------------------------------------
# Checks — each returns a short success note or raises CheckFailed
# ---------------------------------------------------------------------------

def check_wallex() -> str:
    # utils/currency_api.py — primary price source
    payload = _get_json("https://api.wallex.ir/v1/markets")
    if not isinstance(payload, dict) or payload.get("success") is False:
        raise CheckFailed("service reported failure")
    symbols = (payload.get("result") or {}).get("symbols")
    if not symbols:
        raise CheckFailed("no 'result.symbols' in the response")
    return f"{len(symbols)} markets"


def check_ramzinex() -> str:
    # utils/currency_api.py — fallback price source
    payload = _get_json(
        "https://publicapi.ramzinex.com/exchange/api/v1.0/exchange/pairs"
    )
    pairs = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(pairs, list) or not pairs:
        raise CheckFailed("no 'data' list in the response")
    return f"{len(pairs)} pairs"


def check_tgju() -> str:
    # utils/gold_api.py — page scrape; the pattern must match or .gold fails
    body = _get("https://www.tgju.org/profile/geram18")
    html = body.decode("utf-8", errors="replace")
    match = re.search(
        r'<span[^>]*data-col="info\.last_trade\.PDrCotVal"[^>]*>([\d,]+)</span>', html
    )
    if not match:
        raise CheckFailed("page loaded but the price element was not found")
    return f"18k gram = {match.group(1)} rial"


def check_open_meteo_geocoding() -> str:
    # utils/weather_api.py — city name -> coordinates
    payload = _get_json(
        "https://geocoding-api.open-meteo.com/v1/search"
        f"?name={quote(TEST_CITY)}&count=1&language=fa&format=json"
    )
    results = payload.get("results") if isinstance(payload, dict) else None
    if not results:
        raise CheckFailed(f"no result for {TEST_CITY!r}")
    return f"{TEST_CITY} found"


def check_open_meteo_forecast() -> str:
    # utils/weather_api.py — hourly forecast (Tehran's coordinates)
    payload = _get_json(
        "https://api.open-meteo.com/v1/forecast"
        "?latitude=35.69&longitude=51.42"
        "&hourly=temperature_2m,weathercode"
        "&timeformat=unixtime&timezone=auto&forecast_days=2"
    )
    hourly = payload.get("hourly") if isinstance(payload, dict) else None
    if not isinstance(hourly, dict) or not hourly.get("time"):
        raise CheckFailed("no 'hourly' data in the response")
    return f"{len(hourly['time'])} hours"


def check_aladhan() -> str:
    # utils/azan_api.py — answers 200 even on failure; the real code is in the body
    payload = _get_json(
        "https://api.aladhan.com/v1/timingsByAddress"
        f"?address={quote(TEST_CITY)}&method=7"
    )
    if not isinstance(payload, dict) or payload.get("code") != 200:
        code = payload.get("code") if isinstance(payload, dict) else None
        raise CheckFailed(f"service returned code {code!r}")
    timings = (payload.get("data") or {}).get("timings") or {}
    return f"Dhuhr {timings.get('Dhuhr', '?')}"


def check_gemini_key(key: str):
    """
    utils/ai_engines.py — validates one key by listing models.

    Listing models costs no generation quota, so this proves the key is valid
    and the API is enabled for it; it does not prove the daily quota is left.
    """
    def run() -> str:
        payload = _get_json(f"{GEMINI_ROOT}/models?pageSize=200",
                            headers={"x-goog-api-key": key})
        models = payload.get("models") if isinstance(payload, dict) else None
        if not models:
            raise CheckFailed("key accepted but no models listed")
        return f"key valid, {len(models)} models"

    return run


# ---------------------------------------------------------------------------
# Gemini keys from the environment / .env
# ---------------------------------------------------------------------------

def _read_dotenv(path: str) -> dict:
    """Minimal .env reader: KEY=VALUE lines, optional quotes, # comments."""
    values = {}
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return values

    # A quoted value may span several lines (config.py accepts newline-separated
    # keys), so match quoted values first and plain ones second.
    pattern = re.compile(
        r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*'
        r'(?:"([^"]*)"|\'([^\']*)\'|([^\n#]*))',
        re.MULTILINE,
    )
    for match in pattern.finditer(content):
        name = match.group(1)
        value = next(g for g in match.groups()[1:] if g is not None)
        values[name] = value.strip()
    return values


def _gemini_keys() -> list:
    raw = os.environ.get("AI_API_KEYS", "")
    if not raw.strip():
        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        raw = _read_dotenv(env_path).get("AI_API_KEYS", "")
    return [k.strip() for k in raw.replace("\n", ",").split(",") if k.strip()]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def _run(check) -> tuple:
    started = time.monotonic()
    try:
        note = check()
        ok = True
    except CheckFailed as e:
        note = str(e)
        ok = False
    except Exception as e:  # a bug in a check must not hide the other results
        note = f"unexpected error: {type(e).__name__}: {e}"
        ok = False
    return ok, note, time.monotonic() - started


def main() -> int:
    checks = [
        ("Wallex (prices)", check_wallex),
        ("Ramzinex (prices, fallback)", check_ramzinex),
        ("TGJU (gold)", check_tgju),
        ("Open-Meteo geocoding (weather)", check_open_meteo_geocoding),
        ("Open-Meteo forecast (weather)", check_open_meteo_forecast),
        ("AlAdhan (prayer times)", check_aladhan),
    ]

    keys = _gemini_keys()
    for index, key in enumerate(keys, start=1):
        checks.append((f"Gemini key #{index} (...{key[-4:]})", check_gemini_key(key)))

    print("Checking external APIs from this host...\n")

    # All checks at once: the slowest one sets the total time, not the sum.
    with ThreadPoolExecutor(max_workers=len(checks)) as pool:
        results = list(pool.map(lambda item: _run(item[1]), checks))

    width = max(len(name) for name, _ in checks)
    failures = 0
    for (name, _), (ok, note, elapsed) in zip(checks, results):
        mark = "OK  " if ok else "FAIL"
        print(f"  [{mark}] {name:<{width}}  {elapsed:5.1f}s  {note}")
        failures += not ok

    if not keys:
        print("\n  [SKIP] Gemini: AI_API_KEYS is not set in the environment or .env")

    print()
    if failures:
        print(f"{failures} of {len(checks)} checks failed.")
        return 1
    print(f"All {len(checks)} checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
