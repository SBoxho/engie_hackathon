"""Multi-city Open-Meteo weather ingestion with deterministic raw caching."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

import pandas as pd
import requests

from src.config import settings

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
REFERENCE_PATH = Path(__file__).with_name("fr_major_cities_v1.json")
HOURLY_VARIABLES = (
    "temperature_2m",
    "wind_speed_10m",
    "cloud_cover",
    "shortwave_radiation",
    "relative_humidity_2m",
)


class WeatherNationalError(RuntimeError):
    """Raised for an invalid reference or unusable Open-Meteo response."""


@dataclass(frozen=True)
class City:
    id: str
    name: str
    insee_code: str
    latitude: float
    longitude: float
    population: int


def load_city_reference(path: Path = REFERENCE_PATH) -> tuple[list[City], dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise WeatherNationalError("unsupported city reference schema_version")
    cities = [City(**item) for item in payload.get("cities", [])]
    if not cities or len({city.id for city in cities}) != len(cities):
        raise WeatherNationalError("city reference must contain unique city ids")
    if any(city.population <= 0 for city in cities):
        raise WeatherNationalError("city populations must be positive")
    metadata = {key: value for key, value in payload.items() if key != "cities"}
    return cities, metadata


def _date_string(value: date | datetime | str) -> str:
    parsed = pd.Timestamp(value)
    if parsed.tzinfo is not None:
        parsed = parsed.tz_convert("UTC")
    return parsed.date().isoformat()


def _cache_path(cache_dir: Path, city: City, start: str, end: str) -> Path:
    return cache_dir / f"open_meteo_{city.id}_{start}_{end}.json"


def _endpoint_for(end: str, today: date | None = None) -> str:
    today = today or datetime.now(timezone.utc).date()
    return ARCHIVE_URL if date.fromisoformat(end) < today else FORECAST_URL


def clean_city_weather(payload: Mapping, city: City) -> pd.DataFrame:
    hourly = payload.get("hourly", {})
    if not hourly.get("time"):
        raise WeatherNationalError(f"Open-Meteo returned no hourly data for {city.name}")
    frame = pd.DataFrame({name: hourly.get(name) for name in ("time", *HOURLY_VARIABLES)})
    frame = frame.rename(
        columns={
            "time": "timestamp",
            "temperature_2m": "temperature_c",
            "wind_speed_10m": "wind_speed_kmh",
            "cloud_cover": "cloud_cover_pct",
            "shortwave_radiation": "solar_radiation_wm2",
            "relative_humidity_2m": "humidity_pct",
        }
    )
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
    if frame["timestamp"].isna().any():
        raise WeatherNationalError(f"invalid timestamps returned for {city.name}")
    frame.insert(0, "city_id", city.id)
    frame.insert(1, "city_name", city.name)
    frame.insert(2, "population", city.population)
    return frame


def fetch_city_weather(
    city: City,
    start: date | datetime | str,
    end: date | datetime | str,
    *,
    cache_dir: Path | None = None,
    use_cache: bool = True,
    force_refresh: bool = False,
    session=requests,
    today: date | None = None,
) -> pd.DataFrame:
    """Fetch one city/date range; a matching raw JSON cache avoids all network use."""
    start_s, end_s = _date_string(start), _date_string(end)
    if start_s > end_s:
        raise ValueError("start must be on or before end")
    cache_dir = cache_dir or settings.raw_dir / "weather_national"
    path = _cache_path(cache_dir, city, start_s, end_s)
    if use_cache and path.exists() and not force_refresh:
        return clean_city_weather(json.loads(path.read_text(encoding="utf-8")), city)

    params = {
        "latitude": city.latitude,
        "longitude": city.longitude,
        "start_date": start_s,
        "end_date": end_s,
        "hourly": ",".join(HOURLY_VARIABLES),
        "timezone": "UTC",
    }
    response = session.get(_endpoint_for(end_s, today), params=params, timeout=30)
    response.raise_for_status()
    payload = response.json()
    frame = clean_city_weather(payload, city)
    if use_cache:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return frame


def fetch_national_weather(
    start: date | datetime | str,
    end: date | datetime | str,
    *,
    cities: Iterable[City] | None = None,
    cache_dir: Path | None = None,
    use_cache: bool = True,
    force_refresh: bool = False,
    session=requests,
    strict: bool = False,
) -> pd.DataFrame:
    """Fetch cities independently, preserving successes and recording failures."""
    selected = list(cities) if cities is not None else load_city_reference()[0]
    frames: list[pd.DataFrame] = []
    failures: dict[str, str] = {}
    for city in selected:
        try:
            frames.append(
                fetch_city_weather(
                    city, start, end, cache_dir=cache_dir, use_cache=use_cache,
                    force_refresh=force_refresh, session=session,
                )
            )
        except (requests.RequestException, WeatherNationalError, ValueError) as exc:
            if strict:
                raise WeatherNationalError(f"weather fetch failed for {city.name}: {exc}") from exc
            failures[city.id] = str(exc)
    if not frames:
        raise WeatherNationalError(f"no city weather available; failures={failures}")
    result = pd.concat(frames, ignore_index=True).sort_values(["timestamp", "city_id"])
    result.attrs["fetch_failures"] = failures
    return result
