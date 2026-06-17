from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from src.config import settings
from src.demo_mode import external_api_enabled


@dataclass(frozen=True)
class HealthCheck:
    label: str
    status: str
    detail: str


def _has_rows(path: Path) -> tuple[bool, str]:
    if not path.exists():
        return False, "missing"
    try:
        rows = len(pd.read_parquet(path))
    except (OSError, ValueError):
        return False, "unreadable"
    return rows > 0, f"{rows:,} rows"


def _has_json(path: Path) -> tuple[bool, str]:
    if not path.exists():
        return False, "missing"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return False, "unreadable"
    if isinstance(payload, dict):
        generated = payload.get("generated_at")
        return True, f"generated {generated}" if generated else "available"
    return False, "invalid"


def _artifact_status(ok: bool, required: bool) -> str:
    if ok:
        return "ok"
    return "missing" if required else "optional"


def artifact_checks() -> list[HealthCheck]:
    """Return deployment artifact checks without using local-only paths."""
    if settings.is_demo_mode:
        parquet_checks = [
            ("Demo energy", settings.demo_energy_path, True),
            ("Demo weather", settings.demo_weather_path, False),
            ("Demo EcoWatt", settings.demo_ecowatt_path, False),
        ]
        json_checks = [
            ("Demo manifest", settings.demo_dir / "manifest.json", True),
            ("Quality report", settings.demo_quality_path, True),
            ("Demand model evaluation", settings.demo_model_evaluation_path, True),
            ("Model forecast", settings.demo_model_forecast_path, False),
            ("Baseline backtest", settings.demo_baseline_artifact_path, True),
            ("Mood calibration", settings.demo_mood_artifact_path, True),
        ]
    else:
        parquet_checks = [
            ("Processed energy store", settings.energy_store_dir, True),
            ("Weather features", settings.weather_features_path, False),
        ]
        json_checks = [
            ("Baseline backtest", settings.baseline_artifact_path, False),
            ("Mood calibration", settings.mood_artifact_path, False),
            ("Demand model evaluation", settings.processed_dir / "demand_model" / "evaluation.json", False),
        ]

    checks: list[HealthCheck] = []
    for label, path, required in parquet_checks:
        if path.is_dir():
            files = list(path.rglob("*.parquet"))
            ok = bool(files)
            detail = f"{len(files):,} parquet partitions" if ok else "missing"
        else:
            ok, detail = _has_rows(path)
        checks.append(HealthCheck(label, _artifact_status(ok, required), detail))

    for label, path, required in json_checks:
        ok, detail = _has_json(path)
        checks.append(HealthCheck(label, _artifact_status(ok, required), detail))
    return checks


def data_check(data: pd.DataFrame, source_status: str) -> HealthCheck:
    if data.empty:
        return HealthCheck("Data loaded", "missing", "no rows available")
    latest = pd.to_datetime(data["timestamp"].max(), utc=True, errors="coerce")
    latest_text = latest.strftime("%Y-%m-%d %H:%M UTC") if pd.notna(latest) else "unknown timestamp"
    return HealthCheck("Data loaded", "ok", f"{len(data):,} rows, latest {latest_text}; {source_status}")


def mode_check() -> HealthCheck:
    if settings.is_demo_mode:
        api_text = "external APIs disabled" if not external_api_enabled() else "external APIs allowed"
        return HealthCheck("Run mode", "demo", f"APP_MODE=demo, {api_text}")
    return HealthCheck("Run mode", "live", "APP_MODE=live, live fetch/cache fallbacks enabled")


def runtime_checks(
    *,
    data: pd.DataFrame,
    source_status: str,
    weather: pd.DataFrame,
    ecowatt: pd.DataFrame,
    model_payload: dict[str, Any],
    calibration_status: str,
) -> list[HealthCheck]:
    weather_detail = "available" if not weather.empty else "not available for this window"
    ecowatt_detail = "available" if not ecowatt.empty else "not available for this window"
    model_detail = "available" if model_payload else "not available"
    return [
        mode_check(),
        data_check(data, source_status),
        HealthCheck("Weather context", "ok" if not weather.empty else "optional", weather_detail),
        HealthCheck("EcoWatt signal", "ok" if not ecowatt.empty else "optional", ecowatt_detail),
        HealthCheck("Model evaluation", "ok" if model_payload else "optional", model_detail),
        HealthCheck("Mood thresholds", "ok", calibration_status),
        *artifact_checks(),
    ]


def render_deployment_health(
    *,
    data: pd.DataFrame,
    source_status: str,
    weather: pd.DataFrame,
    ecowatt: pd.DataFrame,
    model_payload: dict[str, Any],
    calibration_status: str,
) -> None:
    checks = runtime_checks(
        data=data,
        source_status=source_status,
        weather=weather,
        ecowatt=ecowatt,
        model_payload=model_payload,
        calibration_status=calibration_status,
    )
    failures = [check for check in checks if check.status == "missing"]
    warnings = [check for check in checks if check.status == "optional"]

    with st.sidebar:
        st.markdown("### Deployment health")
        if failures:
            st.error(f"{len(failures)} required check(s) need attention.")
        elif warnings:
            st.warning(f"Ready with {len(warnings)} optional artifact(s) unavailable.")
        else:
            st.success("Ready for public demo.")

        for check in checks:
            icon = {
                "ok": "[ok]",
                "demo": "[demo]",
                "live": "[live]",
                "optional": "[optional]",
                "missing": "[missing]",
            }.get(check.status, "[info]")
            st.caption(f"{icon} **{check.label}** - {check.detail}")
