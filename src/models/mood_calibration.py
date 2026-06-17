"""Transparent, season/hour calibration for the educational grid mood."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

import pandas as pd

TIMEZONE = "Europe/Paris"
REQUIRED_METRICS = (
    "consumption_mw",
    "co2_intensity_g_per_kwh",
    "renewable_share",
    "fossil_share",
)
QUANTILES = {
    "consumption_high": ("consumption_mw", 0.85),
    "co2_low": ("co2_intensity_g_per_kwh", 0.25),
    "co2_high": ("co2_intensity_g_per_kwh", 0.75),
    "renewable_high": ("renewable_share", 0.75),
    "fossil_high": ("fossil_share", 0.75),
}
# These are deliberately only the last resort when no calibrated segment exists.
FIXED_THRESHOLDS = {
    "consumption_high": 70_000.0,
    "co2_low": 40.0,
    "co2_high": 80.0,
    "renewable_high": 0.35,
    "fossil_high": 0.15,
}
PRECEDENCE = ("Carbon-heavy", "Tense", "Renewable-rich", "Calm")
LEVEL_ORDER = ("season_hour", "season", "local_hour", "global")


def season_for_month(month: int) -> str:
    """Return the meteorological season for a month number."""
    if month in (12, 1, 2):
        return "winter"
    if month in (3, 4, 5):
        return "spring"
    if month in (6, 7, 8):
        return "summer"
    if month in (9, 10, 11):
        return "autumn"
    raise ValueError(f"month must be in 1..12, got {month}")


def _utc_iso(value: Any) -> str:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")
    return timestamp.isoformat().replace("+00:00", "Z")


def add_local_segment(frame: pd.DataFrame) -> pd.DataFrame:
    """Add DST-correct Europe/Paris season and wall-clock hour columns."""
    if "timestamp" not in frame:
        raise ValueError("calibration data is missing required column: timestamp")
    result = frame.copy()
    result["timestamp"] = pd.to_datetime(result["timestamp"], utc=True, errors="coerce")
    if result["timestamp"].isna().any():
        raise ValueError("timestamp contains missing or invalid values")
    local = result["timestamp"].dt.tz_convert(TIMEZONE)
    result["season"] = local.dt.month.map(season_for_month)
    result["local_hour"] = local.dt.hour.astype(int)
    return result


def _thresholds(group: pd.DataFrame) -> dict[str, float]:
    return {
        name: float(group[column].quantile(q, interpolation="linear"))
        for name, (column, q) in QUANTILES.items()
    }


def _segment_record(level: str, group: pd.DataFrame, **keys: Any) -> dict[str, Any]:
    return {
        "level": level,
        "season": keys.get("season"),
        "local_hour": keys.get("local_hour"),
        "sample": int(len(group)),
        "thresholds": _thresholds(group),
    }


def calibrate_mood(
    frame: pd.DataFrame,
    *,
    min_sample: int = 30,
    generated_at: datetime | str | None = None,
    source_name: str = "RTE eCO2mix via ODRÉ",
) -> dict[str, Any]:
    """Build a deterministic calibration artifact from complete observations.

    ``generated_at`` should be supplied by reproducible pipelines/tests. It is
    metadata, while all thresholds and source-period fields derive from data.
    """
    if min_sample < 1:
        raise ValueError("min_sample must be at least 1")
    missing = {"timestamp", *REQUIRED_METRICS}.difference(frame.columns)
    if missing:
        raise ValueError(f"calibration data is missing required columns: {sorted(missing)}")

    clean = add_local_segment(frame)
    for column in REQUIRED_METRICS:
        clean[column] = pd.to_numeric(clean[column], errors="coerce")
    clean = clean.dropna(subset=list(REQUIRED_METRICS)).sort_values("timestamp")
    if clean.empty:
        raise ValueError("calibration data has no complete observations")

    segments: list[dict[str, Any]] = []
    for (season, hour), group in clean.groupby(["season", "local_hour"], sort=True):
        segments.append(_segment_record("season_hour", group, season=season, local_hour=int(hour)))
    for season, group in clean.groupby("season", sort=True):
        segments.append(_segment_record("season", group, season=season))
    for hour, group in clean.groupby("local_hour", sort=True):
        segments.append(_segment_record("local_hour", group, local_hour=int(hour)))
    segments.append(_segment_record("global", clean))

    created = generated_at or datetime.now(timezone.utc)
    return {
        "schema_version": 1,
        "timezone": TIMEZONE,
        "season_definition": "meteorological",
        "quantile_method": "pandas linear interpolation",
        "quantiles": {name: q for name, (_, q) in QUANTILES.items()},
        "min_sample": int(min_sample),
        "fallback_order": [*LEVEL_ORDER, "fixed"],
        "precedence": list(PRECEDENCE),
        "fixed_thresholds": FIXED_THRESHOLDS.copy(),
        "source": {
            "name": source_name,
            "rows": int(len(clean)),
            "period_start": _utc_iso(clean["timestamp"].iloc[0]),
            "period_end": _utc_iso(clean["timestamp"].iloc[-1]),
        },
        "generated_at": _utc_iso(created),
        "segments": segments,
    }


def _candidate_keys(season: str, hour: int) -> list[tuple[str, str | None, int | None]]:
    return [
        ("season_hour", season, hour),
        ("season", season, None),
        ("local_hour", None, hour),
        ("global", None, None),
    ]


def _select_thresholds(
    artifact: Mapping[str, Any], season: str, hour: int
) -> tuple[dict[str, float], str, int, str]:
    lookup = {
        (item["level"], item.get("season"), item.get("local_hour")): item
        for item in artifact.get("segments", [])
    }
    minimum = int(artifact.get("min_sample", 1))
    for key in _candidate_keys(season, hour):
        segment = lookup.get(key)
        if segment is not None and int(segment["sample"]) >= minimum:
            level = str(segment["level"])
            fallback = "none" if level == "season_hour" else level
            return dict(segment["thresholds"]), level, int(segment["sample"]), fallback
    return dict(artifact.get("fixed_thresholds", FIXED_THRESHOLDS)), "fixed", 0, "fixed"


def classify_mood(observation: Mapping[str, Any], artifact: Mapping[str, Any]) -> dict[str, Any]:
    """Classify one observation and expose every decision input."""
    missing = {"timestamp", *REQUIRED_METRICS}.difference(observation.keys())
    if missing:
        raise ValueError(f"mood observation is missing required fields: {sorted(missing)}")
    timestamp = pd.to_datetime(observation["timestamp"], utc=True, errors="coerce")
    if pd.isna(timestamp):
        raise ValueError("observation timestamp is invalid")
    local = timestamp.tz_convert(str(artifact.get("timezone", TIMEZONE)))
    season = season_for_month(local.month)
    hour = int(local.hour)
    thresholds, level, sample, fallback = _select_thresholds(artifact, season, hour)
    values = {name: float(observation[name]) for name in REQUIRED_METRICS}

    if (values["co2_intensity_g_per_kwh"] >= thresholds["co2_high"] or
            values["fossil_share"] >= thresholds["fossil_high"]):
        mood = "Carbon-heavy"
        reason = "CO₂ intensity or fossil share is at/above its calibrated upper quartile."
    elif values["consumption_mw"] >= thresholds["consumption_high"]:
        mood = "Tense"
        reason = "Consumption is at/above its calibrated 85th percentile."
    elif (values["renewable_share"] >= thresholds["renewable_high"] and
          values["co2_intensity_g_per_kwh"] <= thresholds["co2_low"]):
        mood = "Renewable-rich"
        reason = "Renewable share is high and CO₂ intensity is at/below its lower quartile."
    else:
        mood = "Calm"
        reason = "No higher-precedence calibrated mood condition is met."

    return {
        "mood": mood,
        "reason": reason,
        "thresholds": thresholds,
        "segment": {"level": level, "season": season, "local_hour": hour},
        "sample": sample,
        "fallback": fallback,
    }

