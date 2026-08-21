"""
Hourly weather from Open-Meteo (no API key needed).

The utils contract: the return value is always a display-ready Persian string
and no exception reaches a handler. An earlier version of this file had no
try/except at all, so a network outage or a small change in the response shape
blew up straight inside Pyrogram's dispatcher.
"""

import logging
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from utils.net import HttpError, fetch_json

log = logging.getLogger(__name__)

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# How many forecast hours are shown.
FORECAST_HOURS = 10

# Error messages — these are shown to the user, hence Persian.
ERROR_NETWORK = "❌ دریافت اطلاعات آب و هوا ممکن نشد؛ کمی بعد دوباره تلاش کن."
ERROR_SCHEMA = "❌ ساختار پاسخ سرویس آب و هوا تغییر کرده است."

WEATHER_CODES = {
    0: "☀️ صاف", 1: "🌤 کمی ابری", 2: "⛅ نیمه ابری", 3: "☁️ ابری",
    45: "🌫 مه‌آلود", 48: "🌫 مه یخ‌زده", 51: "🌧 نم‌نم باران",
    53: "🌧 باران ملایم", 55: "🌧 باران شدید", 56: "🌧 نم‌نم یخ‌زده",
    57: "🌧 باران یخ‌زده", 61: "☔ باران سبک", 63: "☔ باران متوسط",
    65: "☔ باران شدید", 66: "🌧 باران یخ‌زده سبک", 67: "🌧 باران یخ‌زده شدید",
    71: "❄️ برف سبک", 73: "❄️ برف", 75: "❄️ برف شدید", 77: "❄️ دانه برف",
    80: "🌦 رگبار سبک", 81: "🌦 رگبار", 82: "🌦 رگبار شدید",
    85: "🌨 رگبار برف", 86: "🌨 رگبار برف شدید",
    95: "⛈ رعد و برق", 96: "⛈ تگرگ و رعد و برق", 99: "⛈ تگرگ شدید",
}


async def _geocode(city_name: str):
    """
    Turn a city name into coordinates.

    Returns (latitude, longitude, display name), or None if no city matches.
    """
    # quote is required: the city name comes straight from the user's message
    # and may be Persian, contain spaces, or include & and # — which without
    # encoding would break the URL or inject a spurious query parameter.
    #
    # language=fa, not en: Open-Meteo matches the query against the name index of
    # the language asked for, and the users of this bot type Persian city names.
    # With language=en, «تهران», «اصفهان», «شیراز» and «کرج» all returned no
    # result at all, and «مشهد» resolved to "Mashhadī Kolā" — a village in
    # Mazandaran — so the command answered confidently with the wrong city.
    # language=fa resolves Persian and English input alike (verified for both
    # spellings of Tehran/Mashhad and for London) and returns Persian display
    # names, which suits the Persian output anyway.
    url = f"{GEOCODING_URL}?name={quote(city_name)}&count=1&language=fa&format=json"
    payload = await fetch_json(url)

    if not isinstance(payload, dict):
        raise HttpError("geocoding response is not an object")

    results = payload.get("results")
    if not results:
        return None

    first = results[0]
    if not isinstance(first, dict):
        raise HttpError("geocoding result is not an object")

    latitude = first.get("latitude")
    longitude = first.get("longitude")
    if latitude is None or longitude is None:
        raise HttpError("geocoding result has no coordinates")

    return latitude, longitude, first.get("name") or city_name


async def _forecast(latitude, longitude):
    """
    Fetch the hourly forecast.

    Returns (times, temperatures, condition codes, timezone offset).

    timeformat=unixtime is deliberate: it lets us work out which hour in the
    list is "now" without having to reason about the target city's timezone.
    """
    url = (
        f"{FORECAST_URL}?latitude={latitude}&longitude={longitude}"
        "&hourly=temperature_2m,weathercode"
        "&timeformat=unixtime&timezone=auto&forecast_days=2"
    )
    payload = await fetch_json(url)

    if not isinstance(payload, dict):
        raise HttpError("forecast response is not an object")

    hourly = payload.get("hourly")
    if not isinstance(hourly, dict):
        raise HttpError("forecast response has no 'hourly' section")

    times = hourly.get("time")
    temps = hourly.get("temperature_2m")
    codes = hourly.get("weathercode")

    if not (isinstance(times, list) and isinstance(temps, list) and isinstance(codes, list)):
        raise HttpError("forecast arrays are missing or not lists")
    if not times:
        raise HttpError("forecast contains no hours")

    # All three arrays should be the same length; the shortest is used as the
    # bound so a truncated response cannot turn into an IndexError mid-loop.
    usable = min(len(times), len(temps), len(codes))

    offset = payload.get("utc_offset_seconds")
    if not isinstance(offset, (int, float)):
        offset = 0

    return times[:usable], temps[:usable], codes[:usable], int(offset)


def _start_index(times) -> int:
    """
    Index of the first hour that has not passed yet.

    One hour of tolerance is allowed so the current hour (which has only just
    started) is still included. If the whole list is in the past, start at the
    beginning of it.
    """
    now = time.time()
    for index, moment in enumerate(times):
        if isinstance(moment, (int, float)) and moment >= now - 3600:
            return index
    return 0


async def get_10_hours_weather(city_name: str) -> str:
    """
    The next few hours of weather for a city, as display-ready text.

    Never raises; on any failure it returns a Persian message and the real
    cause (in English) stays in the log.
    """
    city_name = (city_name or "").strip()
    if not city_name:
        return "❌ اسم شهر را وارد کن."

    try:
        located = await _geocode(city_name)
    except HttpError as e:
        log.warning("weather: geocoding failed for %r — %s", city_name, e)
        return ERROR_NETWORK

    if located is None:
        return f"❌ شهری به نام «{city_name}» پیدا نشد."

    latitude, longitude, city_display = located

    try:
        times, temps, codes, offset = await _forecast(latitude, longitude)
    except HttpError as e:
        log.warning("weather: forecast failed for %r — %s", city_display, e)
        return ERROR_NETWORK

    start = _start_index(times)
    lines = [f"🌍 **آب و هوای {city_display} تا {FORECAST_HOURS} ساعت آینده:**", ""]

    for index in range(start, min(start + FORECAST_HOURS, len(times))):
        try:
            # Convert the unix timestamp to the city's own local time.
            utc_dt = datetime.fromtimestamp(times[index], timezone.utc)
            local_dt = utc_dt + timedelta(seconds=offset)
            hour = local_dt.strftime("%H:%M")
            temp = round(temps[index])
        except (TypeError, ValueError, OSError, OverflowError) as e:
            # One malformed row must not destroy the whole output.
            log.debug("weather: skipping malformed hour %d — %s", index, e)
            continue

        lines.append(f"⏱ {hour} ⇦ {temp}°C {WEATHER_CODES.get(codes[index], '🌡')}")

    # If every row was malformed, the text is left with no data in it.
    if len(lines) <= 2:
        log.warning("weather: no usable hours for %r", city_display)
        return ERROR_SCHEMA

    return "\n".join(lines)
