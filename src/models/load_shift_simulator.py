from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from src.config import settings

ASSUMPTIONS_PATH = settings.project_root / "data" / "config" / "load_shift_assumptions.json"

PRESSURE_POINTS = {
    "Unknown": 0,
    "Comfortable": 1,
    "Low-carbon opportunity": 1,
    "Watch": 2,
    "Tense": 3,
}

PRESSURE_COLORS = {
    "Unknown": "#9ca3af",
    "Comfortable": "#10b981",
    "Low-carbon opportunity": "#0284c7",
    "Watch": "#f59e0b",
    "Tense": "#ef4444",
}


@dataclass(frozen=True)
class ShiftAction:
    id: str
    label: str
    energy_kwh_per_event: float
    duration_hours: int
    icon: str
    source_label: str
    source_detail: str
    placeholder: bool = False


@dataclass(frozen=True)
class ShiftScore:
    energy_mwh: float
    grid_relief_points: int
    low_carbon_bonus: int
    peak_avoidance_bonus: int
    total_points: int
    co2_delta_kg: float
    demand_delta_mw: float
    original_pressure: str
    shifted_pressure: str


def load_assumption_config(path: Path = ASSUMPTIONS_PATH) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if int(payload.get("schema_version", 0)) != 1:
        raise ValueError("Unsupported load-shift assumption schema")
    if not isinstance(payload.get("actions"), list) or not payload["actions"]:
        raise ValueError("Load-shift assumptions must define at least one action")
    return payload


def load_actions(path: Path = ASSUMPTIONS_PATH) -> dict[str, ShiftAction]:
    payload = load_assumption_config(path)
    actions: dict[str, ShiftAction] = {}
    for item in payload["actions"]:
        action = ShiftAction(
            id=str(item["id"]),
            label=str(item["label"]),
            energy_kwh_per_event=float(item["energy_kwh_per_event"]),
            duration_hours=max(1, int(item["duration_hours"])),
            icon=str(item.get("icon") or ""),
            source_label=str(item.get("source_label") or "Assumption"),
            source_detail=str(item.get("source_detail") or ""),
            placeholder=bool(item.get("placeholder", False)),
        )
        if action.energy_kwh_per_event <= 0:
            raise ValueError(f"Action {action.id} must have positive event energy")
        actions[action.id] = action
    return actions


def build_demo_timeline(
    *,
    start: pd.Timestamp | None = None,
    timezone: str = "Europe/Paris",
) -> pd.DataFrame:
    """Create a labelled offline 24h educational grid context."""
    if start is None:
        start = pd.Timestamp.now(tz="UTC").floor("h") + pd.Timedelta(hours=1)
    start = pd.Timestamp(start)
    start = start.tz_localize("UTC") if start.tzinfo is None else start.tz_convert("UTC")
    targets = pd.date_range(start=start, periods=24, freq="h")
    frame = pd.DataFrame({"target": targets})
    local = frame["target"].dt.tz_convert(timezone)
    frame["local_time"] = local
    frame["hour_label"] = local.dt.strftime("%H:%M")

    hour = local.dt.hour
    morning_peak = hour.between(7, 9).astype(float)
    evening_peak = hour.between(18, 21).astype(float)
    night_valley = hour.between(1, 5).astype(float)
    solar_window = hour.between(11, 15).astype(float)
    frame["demand_signal_mw"] = 48_000 + morning_peak * 9_000 + evening_peak * 15_000 - night_valley * 6_000
    frame["co2_intensity_g_per_kwh"] = 58 + evening_peak * 22 - solar_window * 18 - night_valley * 8

    def status(row: pd.Series) -> str:
        if row["demand_signal_mw"] >= 61_000:
            return "Tense"
        if row["demand_signal_mw"] >= 55_000 or row["co2_intensity_g_per_kwh"] >= 74:
            return "Watch"
        if row["co2_intensity_g_per_kwh"] <= 45:
            return "Low-carbon opportunity"
        return "Comfortable"

    frame["status"] = frame.apply(status, axis=1)
    frame["demand_source"] = "Offline demo profile"
    frame["co2_source"] = "Offline demo profile"
    frame["ecowatt_status"] = "unknown"
    frame["ecowatt_label"] = "Unavailable"
    frame["ecowatt_source"] = "Offline demo"
    return frame


def row_for_local_hour(timeline: pd.DataFrame, hour: int, *, timezone: str) -> pd.Series:
    if timeline.empty:
        raise ValueError("timeline is empty")
    frame = timeline.copy()
    target_column = "target" if "target" in frame else "timestamp"
    frame[target_column] = pd.to_datetime(frame[target_column], utc=True, errors="coerce")
    frame = frame.dropna(subset=[target_column]).sort_values(target_column)
    frame["_local_hour"] = frame[target_column].dt.tz_convert(timezone).dt.hour
    matches = frame.loc[frame["_local_hour"].eq(int(hour))]
    if matches.empty:
        nearest_index = (frame["_local_hour"] - int(hour)).abs().idxmin()
        return frame.loc[nearest_index]
    return matches.iloc[0]


def pressure_value(row: Mapping[str, Any]) -> int:
    return PRESSURE_POINTS.get(str(row.get("status", "Unknown")), 0)


def compute_shift_score(
    action: ShiftAction,
    households: int,
    original: Mapping[str, Any],
    shifted: Mapping[str, Any],
) -> ShiftScore:
    if households < 1:
        raise ValueError("households must be at least 1")
    energy_mwh = action.energy_kwh_per_event * households / 1000
    original_pressure = str(original.get("status", "Unknown"))
    shifted_pressure = str(shifted.get("status", "Unknown"))
    pressure_drop = max(pressure_value(original) - pressure_value(shifted), 0)

    original_demand = pd.to_numeric(original.get("demand_signal_mw"), errors="coerce")
    shifted_demand = pd.to_numeric(shifted.get("demand_signal_mw"), errors="coerce")
    demand_delta_mw = 0.0 if pd.isna(original_demand) or pd.isna(shifted_demand) else float(original_demand - shifted_demand)

    original_co2 = pd.to_numeric(original.get("co2_intensity_g_per_kwh"), errors="coerce")
    shifted_co2 = pd.to_numeric(shifted.get("co2_intensity_g_per_kwh"), errors="coerce")
    co2_delta_kg = 0.0 if pd.isna(original_co2) or pd.isna(shifted_co2) else float((original_co2 - shifted_co2) * energy_mwh)

    grid_relief_points = max(1, round(energy_mwh * (10 + 7 * pressure_drop)))
    low_carbon_bonus = max(0, round(co2_delta_kg / 10))
    peak_avoidance_bonus = 0
    if original_pressure in {"Watch", "Tense"} and pressure_drop > 0:
        peak_avoidance_bonus = 15 + 10 * pressure_drop

    total = grid_relief_points + low_carbon_bonus + peak_avoidance_bonus
    return ShiftScore(
        energy_mwh=energy_mwh,
        grid_relief_points=grid_relief_points,
        low_carbon_bonus=low_carbon_bonus,
        peak_avoidance_bonus=peak_avoidance_bonus,
        total_points=total,
        co2_delta_kg=co2_delta_kg,
        demand_delta_mw=demand_delta_mw,
        original_pressure=original_pressure,
        shifted_pressure=shifted_pressure,
    )


def action_duration_hours(action: ShiftAction) -> list[int]:
    return list(range(max(1, action.duration_hours)))
