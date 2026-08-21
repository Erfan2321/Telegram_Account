"""
Islamic prayer times from AlAdhan.

Like the other utils modules the return value is always a display-ready
Persian string, and no exception ever reaches a handler.
"""

import logging
from urllib.parse import quote

from utils.net import HttpError, fetch_json

log = logging.getLogger(__name__)

TIMINGS_URL = "https://api.aladhan.com/v1/timingsByAddress"

# Method 7: Institute of Geophysics, University of Tehran — the standard
# commonly used in Iran.
CALCULATION_METHOD = 7

ERROR_NETWORK = "❌ دریافت اوقات شرعی ممکن نشد؛ کمی بعد دوباره تلاش کن."
ERROR_SCHEMA = "❌ ساختار پاسخ سرویس اوقات شرعی تغییر کرده است."

# (response key, Persian label)
_ROWS = (
    ("Fajr", "🌅 اذان صبح"),
    ("Sunrise", "☀️ طلوع آفتاب"),
    ("Dhuhr", "🌞 اذان ظهر"),
    ("Maghrib", "🌙 اذان مغرب"),
    ("Midnight", "🌌 نیمه‌شب شرعی"),
)


async def get_prayer_times(city_name: str) -> str:
    """Today's prayer times for a given city."""
    city_name = (city_name or "").strip()
    if not city_name:
        return "❌ اسم شهر را وارد کن."

    # quote is required: the city name comes from the user's message and may
    # be Persian or several words long ("تربت حیدریه", "New York").
    url = f"{TIMINGS_URL}?address={quote(city_name)}&method={CALCULATION_METHOD}"

    try:
        payload = await fetch_json(url)
    except HttpError as e:
        log.warning("azan: request failed for %r — %s", city_name, e)
        return ERROR_NETWORK

    if not isinstance(payload, dict):
        log.warning("azan: response is not an object")
        return ERROR_SCHEMA

    # The service answers 200 even for an unknown city and puts the real
    # status in the body, so checking the HTTP status alone is not enough.
    if payload.get("code") != 200:
        log.info("azan: service returned code %r for %r", payload.get("code"), city_name)
        return f"❌ شهر «{city_name}» پیدا نشد."

    timings = (payload.get("data") or {}).get("timings")
    if not isinstance(timings, dict):
        log.warning("azan: response has no 'data.timings' section")
        return ERROR_SCHEMA

    lines = [f"🕌 **اوقات شرعی به افق {city_name}:**", ""]
    for key, label in _ROWS:
        lines.append(f"{label}: {timings.get(key) or 'نامشخص'}")

    return "\n".join(lines)
