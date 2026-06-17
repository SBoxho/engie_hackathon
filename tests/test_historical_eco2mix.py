from __future__ import annotations

import os
from datetime import datetime, timezone

import pandas as pd
import pytest

from src.data_sources.rte_eco2mix_historical import (
    DATASET_ID,
    HistoricalEco2MixError,
    fetch_historical_raw,
    reconcile_historical_schema,
    to_clean_energy_mix,
    validate_range,
)


def record(timestamp: str, consumption: int = 50_000) -> dict:
    return {
        "date_heure": timestamp,
        "perimetre": "France",
        "consommation": consumption,
        "nucleaire": 35_000,
        "eolien": 5_000,
        "solaire": 2_000,
        "hydraulique": 6_000,
        "gaz": 2_000,
        "charbon": 0,
        "fioul": 0,
        "bioenergies": 1_000,
        "ech_physiques": -1_000,
        "taux_co2": 28,
    }


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class PagingSession:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return Response(next(self.pages))


def test_paginates_deduplicates_and_uses_half_open_boundaries(tmp_path):
    first = [record(f"2024-01-01T{hour:02d}:00:00Z", hour) for hour in range(100)]
    # Use valid hours while retaining enough synthetic rows to force pagination.
    first = [record(f"2024-01-01T{index // 2:02d}:{(index % 2) * 30:02d}:00Z", index) for index in range(48)]
    first += [record("2024-01-02T00:00:00Z", 999)] * 52
    second = [record("2024-01-01T23:30:00Z", 123), record("2024-01-02T00:00:00Z", 999)]
    session = PagingSession([
        {"total_count": 102, "results": first},
        {"total_count": 102, "results": second},
    ])
    frame = fetch_historical_raw(
        "2024-01-01", "2024-01-02", session=session, cache_dir=tmp_path
    )
    assert len(session.calls) == 2
    assert session.calls[0][1]["params"]["limit"] == 100
    assert session.calls[1][1]["params"]["offset"] == 100
    assert "date_heure <" in session.calls[0][1]["params"]["where"]
    assert frame["date_heure"].max() < pd.Timestamp("2024-01-02", tz="UTC")
    assert frame["date_heure"].is_unique
    snapshots = list(tmp_path.glob(f"{DATASET_ID}_*.json"))
    assert len(snapshots) == 1
    original_mtime = snapshots[0].stat().st_mtime_ns

    repeat = PagingSession([
        {"total_count": 102, "results": first},
        {"total_count": 102, "results": second},
    ])
    fetch_historical_raw("2024-01-01", "2024-01-02", session=repeat, cache_dir=tmp_path)
    assert len(list(tmp_path.glob(f"{DATASET_ID}_*.json"))) == 1
    assert snapshots[0].stat().st_mtime_ns == original_mtime


def test_reconciles_labels_and_standardizes_with_deduplication():
    raw = pd.DataFrame([
        record("2024-01-01T00:00:00Z", 100),
        record("2024-01-01T00:00:00Z", 200),
    ]).rename(columns={"ech_physiques": "Échanges physiques", "taux_co2": "Taux CO2"})
    reconciled = reconcile_historical_schema(raw)
    assert {"ech_physiques", "taux_co2", "fioul"} <= set(reconciled.columns)
    clean = to_clean_energy_mix(raw)
    assert len(clean) == 1
    assert clean.iloc[0]["consumption_mw"] == 200
    assert clean.iloc[0]["region"] == "France"


def test_longer_interval_is_split_into_bounded_chunks():
    session = PagingSession([
        {"total_count": 1, "results": [record("2024-01-01T12:00:00Z")]},
        {"total_count": 1, "results": [record("2024-01-02T12:00:00Z")]},
    ])
    frame = fetch_historical_raw(
        "2024-01-01", "2024-01-03", session=session, cache=False, chunk_days=1
    )
    assert len(frame) == 2
    assert len(session.calls) == 2
    first_where = session.calls[0][1]["params"]["where"]
    second_where = session.calls[1][1]["params"]["where"]
    assert "2024-01-02T00:00:00Z" in first_where
    assert "2024-01-02T00:00:00Z" in second_where


def test_empty_response_returns_empty_standard_contract(tmp_path):
    session = PagingSession([{"total_count": 0, "results": []}])
    raw = fetch_historical_raw(
        "2024-01-01", "2024-01-02", session=session, cache=False
    )
    clean = to_clean_energy_mix(raw)
    assert raw.empty and clean.empty
    assert {"timestamp", "consumption_mw", "renewable_share"} <= set(clean.columns)


@pytest.mark.parametrize(
    "start,end,message",
    [
        ("2011-12-31", "2012-01-02", "starts on"),
        ("2024-01-02", "2024-01-02", "earlier"),
        ("2024-01-01", "2025-01-02", "at most"),
    ],
)
def test_range_validation(start, end, message):
    with pytest.raises(ValueError, match=message):
        validate_range(start, end)


def test_invalid_api_shape_is_explicit():
    session = PagingSession([{"total_count": 1}])
    with pytest.raises(HistoricalEco2MixError, match="results"):
        fetch_historical_raw("2024-01-01", "2024-01-02", session=session, cache=False)


@pytest.mark.skipif(
    os.getenv("RUN_REAL_DATA_TESTS") != "1",
    reason="offline-safe: set RUN_REAL_DATA_TESTS=1 to query official ODRÉ",
)
def test_real_odre_historical_smoke():
    frame = fetch_historical_raw(
        datetime(2024, 1, 1, tzinfo=timezone.utc),
        datetime(2024, 1, 1, 2, tzinfo=timezone.utc),
        cache=False,
    )
    assert not frame.empty
    assert {"date_heure", "consommation", "nucleaire"} <= set(frame.columns)
