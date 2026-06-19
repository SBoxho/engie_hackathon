from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
import streamlit as st

from app.view_models import synthesize_regional_history
from src.config import settings
from src.data_processing.clean_energy_mix import clean_energy_mix
from src.data_processing.features import add_time_features
from src.data_processing.storage import PartitionedParquetStore
from src.data_sources.ecowatt import load_ecowatt_window
from src.data_sources.rte_eco2mix import Eco2MixError, fetch_eco2mix, load_cached_eco2mix
from src.data_sources.rte_eco2mix_regional import (
    REGION_NAMES,
    RegionalEco2MixError,
    demo_regional_snapshot,
    fallback_region_geojson,
    fetch_regional_eco2mix,
    load_cached_regional_eco2mix,
    load_region_geojson,
    prepare_regional_snapshot,
    region_code,
)
from src.demo_mode import (
    demo_ecowatt,
    demo_energy,
    demo_model_evaluation,
    demo_mood_artifact,
    external_api_enabled,
    read_demo_parquet,
)

REGIONAL_CONTEXT_CACHE_VERSION = 3
REPLAY_TIMEBASE_CACHE_VERSION = 2


def _mode_for_source(*, live: bool) -> str:
    return "LIVE" if live else "REPLAY"


def _replay_anchor() -> pd.Timestamp:
    demo = demo_energy()
    if not demo.empty and "timestamp" in demo:
        latest = pd.to_datetime(demo["timestamp"], utc=True, errors="coerce").dropna()
        if not latest.empty:
            return pd.Timestamp(latest.max()).floor("15min")
    return pd.Timestamp.now(tz="UTC").floor("15min")


@st.cache_data(ttl=900, show_spinner=False)
def load_national_energy(
    hours: int,
    replay_timebase_version: int = REPLAY_TIMEBASE_CACHE_VERSION,
) -> tuple[pd.DataFrame, str, str]:
    _ = replay_timebase_version
    if settings.is_demo_mode and not external_api_enabled():
        demo = demo_energy()
        if not demo.empty:
            return demo.sort_values("timestamp"), "Bundled demo replay sample", _mode_for_source(live=False)
        return pd.DataFrame(), "Historical sample unavailable", _mode_for_source(live=False)

    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    try:
        raw = fetch_eco2mix(start=start, end=end)
        clean = add_time_features(clean_energy_mix(raw), settings.timezone)
        PartitionedParquetStore(settings.energy_store_dir).upsert(clean)
        return clean.sort_values("timestamp"), "RTE eco2mix refreshed", _mode_for_source(live=True)
    except (Eco2MixError, OSError, ValueError):
        try:
            stored = PartitionedParquetStore(settings.energy_store_dir).read(start=start, end=end)
            if not stored.empty:
                return stored.sort_values("timestamp"), "RTE eco2mix cached snapshot", _mode_for_source(live=False)
        except (OSError, ValueError):
            pass
        raw = load_cached_eco2mix()
        return clean_energy_mix(raw).sort_values("timestamp"), "RTE eco2mix cached snapshot", _mode_for_source(live=False)


@st.cache_data(ttl=900, show_spinner=False)
def load_weather(
    start: pd.Timestamp,
    end: pd.Timestamp,
    replay_timebase_version: int = REPLAY_TIMEBASE_CACHE_VERSION,
) -> pd.DataFrame:
    _ = replay_timebase_version
    if settings.is_demo_mode and not external_api_enabled():
        weather = read_demo_parquet(settings.demo_weather_path)
        if weather.empty:
            return weather
        return weather.loc[weather["timestamp"].between(start, end)].copy()
    if not settings.weather_features_path.exists():
        return pd.DataFrame()
    weather = pd.read_parquet(settings.weather_features_path)
    if "timestamp" not in weather:
        return pd.DataFrame()
    weather["timestamp"] = pd.to_datetime(weather["timestamp"], utc=True, errors="coerce")
    return weather.loc[weather["timestamp"].between(start, end)].copy()


# Paris is used as the live "current weather" reference for the Now page:
# a single Open-Meteo call (no API key) keeps the live-weather payload cheap
# while remaining representative of the population-weighted demand center.
_LIVE_WEATHER_LAT = 48.8566
_LIVE_WEATHER_LON = 2.3522
_LIVE_WEATHER_URL = "https://api.open-meteo.com/v1/forecast"


@st.cache_data(ttl=900, show_spinner=False)
def load_live_current_weather() -> dict[str, Any] | None:
    """Return Open-Meteo current weather for Paris, or None when unavailable.

    Cached for 15 minutes. Open-Meteo is keyless and free, and current
    weather is not replay-sensitive (unlike historical demand fixtures), so
    we attempt the call even in demo mode — otherwise the page would show
    the bundled winter parquet's temperature in mid-summer. On any network
    or parse error we return None and callers fall back to the bundled
    weather snapshot.
    """
    try:
        import requests

        response = requests.get(
            _LIVE_WEATHER_URL,
            params={
                "latitude": _LIVE_WEATHER_LAT,
                "longitude": _LIVE_WEATHER_LON,
                "current": "temperature_2m,wind_speed_10m,cloud_cover",
                "wind_speed_unit": "kmh",
                "timezone": "UTC",
            },
            timeout=8,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception:  # noqa: BLE001 — any failure means we fall back to bundled weather
        return None
    current = payload.get("current") or {}
    if not current:
        return None
    return {
        "temperature_c": current.get("temperature_2m"),
        "wind_kmh": current.get("wind_speed_10m"),
        "cloud_pct": current.get("cloud_cover"),
        "observed_at": current.get("time"),
        "location": "Paris",
        "source": "Open-Meteo",
    }


@st.cache_data(ttl=900, show_spinner=False)
def load_live_weather_forecast() -> pd.DataFrame:
    """Return Open-Meteo hourly forecast for Paris over the next ~3 days.

    Mirrors :func:`load_live_current_weather` but pulls the ``hourly`` block so
    the Next 48H page can show selected-hour weather context next to the
    confidence factors. Cached for 15 minutes; on any failure we return an
    empty frame and callers fall back to live current weather or the bundled
    snapshot.
    """
    try:
        import requests

        response = requests.get(
            _LIVE_WEATHER_URL,
            params={
                "latitude": _LIVE_WEATHER_LAT,
                "longitude": _LIVE_WEATHER_LON,
                "hourly": "temperature_2m,wind_speed_10m,cloud_cover",
                "wind_speed_unit": "kmh",
                "timezone": "UTC",
                "forecast_days": 3,
            },
            timeout=8,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception:  # noqa: BLE001 — any failure falls back to live/bundled weather
        return pd.DataFrame()
    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    if not times:
        return pd.DataFrame()
    frame = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(times, utc=True, errors="coerce"),
            "temperature_c": hourly.get("temperature_2m"),
            "wind_kmh": hourly.get("wind_speed_10m"),
            "cloud_pct": hourly.get("cloud_cover"),
        }
    )
    frame = frame.dropna(subset=["timestamp"]).reset_index(drop=True)
    frame["location"] = "Paris"
    frame["source"] = "Open-Meteo"
    return frame


@st.cache_data(ttl=900, show_spinner=False)
def load_ecowatt(start: pd.Timestamp, end: pd.Timestamp) -> tuple[pd.DataFrame, str]:
    if settings.is_demo_mode and not external_api_enabled():
        return demo_ecowatt(start, end)
    return load_ecowatt_window(start, end, timezone_name=settings.timezone)


@st.cache_data(ttl=900, show_spinner=False)
def load_model_evaluation() -> dict[str, Any]:
    if settings.is_demo_mode:
        return demo_model_evaluation()
    path = settings.processed_dir / "demand_model" / "evaluation.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


@st.cache_data(ttl=900, show_spinner=False)
def load_mood_artifact() -> dict[str, Any]:
    if settings.is_demo_mode:
        return demo_mood_artifact()
    if not settings.mood_artifact_path.exists():
        return {}
    try:
        payload = json.loads(settings.mood_artifact_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _regional_history_from_raw(raw: pd.DataFrame) -> pd.DataFrame:
    clean = clean_energy_mix(raw)
    if "region" not in clean:
        return pd.DataFrame()
    clean["region_code"] = clean["region"].map(region_code)
    clean = clean.dropna(subset=["region_code"]).copy()
    if clean.empty:
        return clean
    clean["region_code"] = clean["region_code"].astype(str)
    clean["region_display"] = clean["region_code"].map(REGION_NAMES).fillna(clean["region"])
    return clean.sort_values(["region_code", "timestamp"]).reset_index(drop=True)


@st.cache_data(ttl=900, show_spinner=False)
def load_regional_energy(
    hours: int,
    cache_version: int = REGIONAL_CONTEXT_CACHE_VERSION,
) -> tuple[pd.DataFrame, pd.DataFrame, str, str]:
    _ = cache_version
    if settings.is_demo_mode and not external_api_enabled():
        snapshot = demo_regional_snapshot(_replay_anchor().floor("h"))
        history = synthesize_regional_history(snapshot, timezone=settings.timezone)
        return snapshot, history, "Regional historical sample", _mode_for_source(live=False)

    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    try:
        raw = fetch_regional_eco2mix(start=start, end=end)
        snapshot = prepare_regional_snapshot(raw)
        history = _regional_history_from_raw(raw)
        return snapshot, history, "RTE regional eco2mix refreshed", _mode_for_source(live=True)
    except (RegionalEco2MixError, OSError, ValueError):
        try:
            raw = load_cached_regional_eco2mix()
            snapshot = prepare_regional_snapshot(raw)
            history = _regional_history_from_raw(raw)
            return snapshot, history, "RTE regional eco2mix cached snapshot", _mode_for_source(live=False)
        except (RegionalEco2MixError, FileNotFoundError, ValueError, OSError):
            snapshot = demo_regional_snapshot()
            history = synthesize_regional_history(snapshot, timezone=settings.timezone)
            return snapshot, history, "Regional historical sample", _mode_for_source(live=False)


@st.cache_data(ttl=86400, show_spinner=False)
def load_regions_geojson() -> tuple[dict[str, Any], str]:
    if settings.is_demo_mode and not external_api_enabled():
        return fallback_region_geojson(), "Bundled regional boundaries"
    try:
        return load_region_geojson(), "French administrative regional boundaries"
    except (RegionalEco2MixError, AttributeError, TypeError, ValueError, OSError):
        return fallback_region_geojson(), "Bundled regional boundaries"


def load_public_context() -> dict[str, Any]:
    energy, national_source, national_mode = load_national_energy(settings.history_hours)
    if energy.empty:
        return {
            "energy": energy,
            "national_source": national_source,
            "mode": national_mode,
            "weather": pd.DataFrame(),
            "ecowatt": pd.DataFrame(),
            "ecowatt_source": "Unavailable",
            "model_payload": {},
            "regional": pd.DataFrame(),
            "regional_history": pd.DataFrame(),
            "regions_geojson": {"type": "FeatureCollection", "features": []},
            "regional_source": "Unavailable",
        }

    start = pd.to_datetime(energy["timestamp"].min(), utc=True)
    end = pd.to_datetime(energy["timestamp"].max(), utc=True)
    weather = load_weather(start, end)
    ecowatt_start = end.floor("h") - pd.Timedelta(hours=1)
    ecowatt_end = end.floor("h") + pd.Timedelta(hours=49)
    ecowatt, ecowatt_source = load_ecowatt(ecowatt_start, ecowatt_end)
    regional, regional_history, regional_source, regional_mode = load_regional_energy(settings.history_hours)
    regions_geojson, geo_source = load_regions_geojson()
    mode = "LIVE" if national_mode == "LIVE" and regional_mode == "LIVE" else "REPLAY"
    return {
        "energy": energy,
        "national_source": national_source,
        "mode": mode,
        "weather": weather,
        "ecowatt": ecowatt,
        "ecowatt_source": ecowatt_source,
        "model_payload": load_model_evaluation(),
        "mood_artifact": load_mood_artifact(),
        "regional": regional,
        "regional_history": regional_history,
        "regions_geojson": regions_geojson,
        "regional_source": regional_source,
        "geo_source": geo_source,
    }
