"""Weather features from Open-Meteo (free, no API key)."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import requests

from src.config import settings
from src.utils.io import read_json, timestamped_path, write_json


def fetch_weather(
    latitude: float = 46.603354,
    longitude: float = 1.888334,
    *,
    forecast_days: int = 2,
    cache: bool = True,
) -> pd.DataFrame:
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "hourly": "temperature_2m,wind_speed_10m,cloud_cover,shortwave_radiation,relative_humidity_2m",
        "forecast_days": forecast_days,
        "timezone": "UTC",
    }
    response = requests.get(settings.open_meteo_base_url, params=params, timeout=30)
    response.raise_for_status()
    payload = response.json()
    hourly = payload.get("hourly", {})
    if not hourly.get("time"):
        raise ValueError("Open-Meteo returned no hourly weather data")
    if cache:
        write_json(payload, timestamped_path(settings.raw_dir / "weather", "open_meteo"))
    return clean_weather(payload)


def load_cached_weather(path: Path) -> pd.DataFrame:
    return clean_weather(read_json(path))


def clean_weather(payload: dict) -> pd.DataFrame:
    hourly = payload.get("hourly", {})
    frame = pd.DataFrame(hourly)
    frame = frame.rename(
        columns={
            "time": "timestamp",
            "temperature_2m": "temperature_c",
            "wind_speed_10m": "wind_speed",
            "shortwave_radiation": "solar_radiation",
            "relative_humidity_2m": "humidity",
        }
    )
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
    return frame

