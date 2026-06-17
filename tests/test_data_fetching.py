from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.data_sources.rte_eco2mix import Eco2MixError, fetch_eco2mix, load_cached_eco2mix


def record(timestamp="2026-06-17T10:00:00+00:00"):
    return {
        "date_heure": timestamp, "perimetre": "France", "consommation": 50000,
        "nucleaire": 39000, "eolien": 4000, "solaire": 7000,
        "hydraulique": 5000, "gaz": 1000, "charbon": 0, "fioul": 20,
        "bioenergies": 900, "ech_physiques": -6900, "taux_co2": 22,
    }


class FakeResponse:
    def __init__(self, payload, status_error=None):
        self.payload = payload
        self.status_error = status_error

    def raise_for_status(self):
        if self.status_error:
            raise self.status_error

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, response):
        self.response = response

    def get(self, *args, **kwargs):
        return self.response


def test_fetches_and_caches_raw_records():
    cache_dir = Path("data/interim/test_cache")
    session = FakeSession(FakeResponse({"total_count": 1, "results": [record()]}))
    frame = fetch_eco2mix(
        datetime(2026, 6, 17, 9, tzinfo=timezone.utc),
        datetime(2026, 6, 17, 11, tzinfo=timezone.utc),
        cache_dir=cache_dir,
        session=session,
    )
    assert len(frame) == 1
    assert list(cache_dir.glob("*.json"))
    assert len(load_cached_eco2mix(cache_dir=cache_dir)) == 1


def test_rejects_empty_response():
    session = FakeSession(FakeResponse({"total_count": 0, "results": []}))
    with pytest.raises(Eco2MixError, match="no populated records"):
        fetch_eco2mix(
            datetime(2026, 6, 17, 9, tzinfo=timezone.utc),
            datetime(2026, 6, 17, 11, tzinfo=timezone.utc),
            cache=False,
            session=session,
        )
