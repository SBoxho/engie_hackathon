from __future__ import annotations

import os
from datetime import datetime, timezone

import pandas as pd
import pytest

from src.data_processing.clean_energy_mix import clean_energy_mix
from src.data_processing.quality import REQUIRED_COLUMNS, run_quality_checks
from src.data_sources.rte_eco2mix import fetch_eco2mix


def valid_frame(periods: int = 3) -> pd.DataFrame:
    timestamp = pd.date_range("2026-06-17T10:00Z", periods=periods, freq="15min")
    frame = pd.DataFrame(
        {
            "timestamp": timestamp,
            "region": "France",
            "consumption_mw": 50_000.0,
            "nuclear_mw": 35_000.0,
            "wind_mw": 5_000.0,
            "solar_mw": 3_000.0,
            "hydro_mw": 4_000.0,
            "gas_mw": 2_000.0,
            "coal_mw": 0.0,
            "oil_mw": 0.0,
            "bioenergy_mw": 1_000.0,
            "imports_mw": 0.0,
            "exports_mw": 0.0,
            "total_production_mw": 50_000.0,
            "renewable_share": 0.26,
            "fossil_share": 0.04,
            "co2_intensity_g_per_kwh": 40.0,
        }
    )
    return frame


def finding(report, name):
    return next(item for item in report.findings if item.check == name)


def test_valid_frame_has_no_errors_or_warnings():
    frame = valid_frame()
    report = run_quality_checks(frame, now=datetime(2026, 6, 17, 11, tzinfo=timezone.utc))
    assert report.passed
    assert {f.severity for f in report.findings} == {"info"}
    assert report.suspicious_rows.empty


def test_classifies_bad_rows_and_preserves_evidence():
    frame = valid_frame(4)
    frame.loc[1, "timestamp"] = frame.loc[0, "timestamp"]
    frame.loc[2, "timestamp"] = pd.Timestamp("2026-06-17T11:00Z")
    frame.loc[1, "wind_mw"] = -5
    frame.loc[2, "renewable_share"] = 1.2
    frame.loc[3, "consumption_mw"] = 500_000
    frame.loc[3, "total_production_mw"] = 1
    frame.loc[0, "solar_mw"] = None

    report = run_quality_checks(frame, now=datetime(2026, 6, 18, tzinfo=timezone.utc))
    assert not report.passed
    assert finding(report, "duplicate_keys").count == 2
    assert finding(report, "missing_intervals").count >= 1
    assert finding(report, "nonnegative_generation").severity == "error"
    assert finding(report, "share_bounds").severity == "error"
    assert finding(report, "stale_latest_timestamp").severity == "warning"
    assert finding(report, "extreme_values").severity == "warning"
    assert finding(report, "balance_residual").severity == "warning"
    assert {"_quality_check", "_quality_severity", "_source_index"} <= set(report.suspicious_rows)
    assert len(frame) == 4


def test_schema_and_malformed_timestamps_are_explicit():
    frame = pd.DataFrame({"timestamp": ["bad", None], "region": ["France", "France"]})
    report = run_quality_checks(frame, required_columns=REQUIRED_COLUMNS)
    assert finding(report, "schema").count == len(REQUIRED_COLUMNS) - 2
    assert finding(report, "timestamp_validity").count == 2
    assert not report.passed


@pytest.mark.skipif(os.getenv("RUN_LIVE_DATA_SMOKE") != "1", reason="offline-safe; opt in to official API")
def test_official_eco2mix_live_smoke():
    end = datetime.now(timezone.utc)
    raw = fetch_eco2mix(history_hours=2, cache=False, timeout=30)
    clean = clean_energy_mix(raw)
    assert not clean.empty
    assert set(REQUIRED_COLUMNS) <= set(clean.columns)
