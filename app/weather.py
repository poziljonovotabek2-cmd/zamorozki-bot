import json
from datetime import date, datetime, timedelta, timezone
from math import ceil

import httpx
from redis.asyncio import Redis

from app.config import settings

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"


def cache_key(prefix: str, latitude: float, longitude: float) -> str:
    return f"{prefix}:{latitude:.3f}:{longitude:.3f}"


async def cached_json(redis: Redis, key: str) -> dict | None:
    value = await redis.get(key)
    return json.loads(value) if value else None


async def put_json(redis: Redis, key: str, value: dict, ttl: int) -> None:
    await redis.set(key, json.dumps(value), ex=ttl)


async def geocode(query: str, language: str) -> dict | None:
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(GEOCODE_URL, params={"name": query, "count": 1, "language": language, "format": "json"})
        response.raise_for_status()
        results = response.json().get("results") or []
    return results[0] if results else None


async def forecast(redis: Redis, latitude: float, longitude: float) -> dict:
    key = cache_key("forecast", latitude, longitude)
    cached = await cached_json(redis, key)
    if cached:
        return cached
    async with httpx.AsyncClient(timeout=25) as client:
        response = await client.get(FORECAST_URL, params={
            "latitude": latitude, "longitude": longitude,
            "hourly": "temperature_2m", "forecast_days": 16,
            "timezone": "auto", "temperature_unit": "celsius",
        })
        response.raise_for_status()
        data = response.json()
    await put_json(redis, key, data, 20 * 60)
    return data


async def archive(redis: Redis, latitude: float, longitude: float, today: date) -> dict:
    first_year, last_year = today.year - 10, today.year - 1
    key = f"{cache_key('archive', latitude, longitude)}:{first_year}:{last_year}"
    cached = await cached_json(redis, key)
    if cached:
        return cached
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.get(ARCHIVE_URL, params={
            "latitude": latitude, "longitude": longitude,
            "start_date": f"{first_year}-01-01", "end_date": f"{last_year}-12-31",
            "hourly": "temperature_2m", "timezone": "auto", "temperature_unit": "celsius",
        })
        response.raise_for_status()
        data = response.json()
    await put_json(redis, key, data, 30 * 24 * 60 * 60)
    return data


def local_now(data: dict) -> datetime:
    zone = data.get("timezone", "Asia/Tashkent")
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(zone))
    except Exception:
        return datetime.now(timezone.utc) + timedelta(seconds=int(data.get("utc_offset_seconds", 0)))


def forecast_risks(data: dict) -> list[dict]:
    times = data.get("hourly", {}).get("time", [])
    temps = data.get("hourly", {}).get("temperature_2m", [])
    if not times or not temps:
        return []
    now = local_now(data).replace(tzinfo=None)
    start, end = now + timedelta(hours=24), now + timedelta(hours=48)
    by_day: dict[date, list[float]] = {}
    for stamp, temp in zip(times, temps):
        if temp is None:
            continue
        at = datetime.fromisoformat(stamp)
        if start <= at <= end:
            by_day.setdefault(at.date(), []).append(float(temp))
    risks = []
    for day, values in sorted(by_day.items()):
        minimum = min(values)
        if minimum <= 2:
            risks.append({"date": day.isoformat(), "minimum": round(minimum, 1), "severity": "freeze" if minimum <= 0 else "watch"})
    return risks


def day_number(value: date) -> int:
    return date(2001, value.month, value.day).timetuple().tm_yday


def quantile(values: list[int], fraction: float) -> int:
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, ceil(fraction * len(ordered)) - 1))]


def history_summary(data: dict) -> dict:
    hourly = data.get("hourly", {})
    times, temps = hourly.get("time", []), hourly.get("temperature_2m", [])
    samples: dict[date, list[float]] = {}
    for stamp, temp in zip(times, temps):
        if temp is None:
            continue
        local_day = datetime.fromisoformat(stamp).date()
        samples.setdefault(local_day, []).append(float(temp))
    # Ignore partial days: one surviving warm hour must not classify a day as frost-free.
    daily = {day: min(values) for day, values in samples.items() if len(values) >= 18}
    years = sorted({datetime.fromisoformat(stamp).year for stamp in times})
    spring_last: list[int] = []
    autumn_first: list[int] = []
    for year in years:
        spring_days = {day for day in daily if day.year == year and date(year, 3, 1) <= day <= date(year, 5, 31)}
        autumn_days = {day for day in daily if day.year == year and date(year, 9, 1) <= day <= date(year, 11, 30)}
        # Drop incomplete seasons so missing API observations cannot look like frost-free weather.
        if len(spring_days) < 88 or len(autumn_days) < 87:
            continue
        spring = [day for day in spring_days if daily[day] <= 0]
        autumn = [day for day in autumn_days if daily[day] <= 0]
        spring_last.append(day_number(max(spring)) if spring else day_number(date(year, 2, 28)))
        autumn_first.append(day_number(min(autumn)) if autumn else day_number(date(year, 12, 1)))
    n = min(len(spring_last), len(autumn_first))
    if n == 0:
        return {"years": 0, "spring": None, "autumn": None}
    # First ordinal day after the 90th percentile of the last spring freeze.
    spring_cutoff = min(365, quantile(spring_last, 0.9) + 1)
    # The autumn harvest deadline precedes the 10th percentile of first freezes.
    autumn_cutoff = max(1, quantile(autumn_first, 0.1) - 1)
    usable = min(len(spring_last), len(autumn_first))
    spring_safe = sum(last < spring_cutoff for last in spring_last)
    autumn_safe = sum(first > autumn_cutoff for first in autumn_first)
    return {
        "years": usable,
        "spring": {"ordinal": spring_cutoff, "safe_count": spring_safe},
        "autumn": {"ordinal": autumn_cutoff, "safe_count": autumn_safe},
    }


def ordinal_date(ordinal: int) -> date:
    return date(2001, 1, 1) + timedelta(days=ordinal - 1)
