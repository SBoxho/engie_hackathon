from __future__ import annotations

import json
from datetime import date

import pandas as pd
import pytest

from src.data_processing.weather_features import (
    align_city_weather,
    build_national_weather_features,
    join_energy_weather,
    population_weighted_features,
)
from src.data_sources.weather_national import City, fetch_city_weather, load_city_reference


PARIS = City("paris", "Paris", "75056", 48.8566, 2.3522, 2_000_000)
LYON = City("lyon", "Lyon", "69123", 45.764, 4.8357, 1_000_000)


def payload(times=("2024-01-15T00:00", "2024-01-15T01:00"), base=10.0):
    count = len(times)
    return {
        "hourly": {
            "time": list(times),
            "temperature_2m": [base + index for index in range(count)],
            "wind_speed_10m": [20.0] * count,
            "cloud_cover": [30.0] * count,
            "shortwave_radiation": [40.0] * count,
            "relative_humidity_2m": [50.0] * count,
        }
    }


class Response:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self.body


class Session:
    def __init__(self, body, fail=False):
        self.body, self.fail, self.calls = body, fail, []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.fail:
            raise AssertionError("network must not be called")
        return Response(self.body)


def raw_city(city, base):
    frame = pd.DataFrame(payload(base=base)["hourly"]).rename(
        columns={
            "time": "timestamp", "temperature_2m": "temperature_c",
            "wind_speed_10m": "wind_speed_kmh", "cloud_cover": "cloud_cover_pct",
            "shortwave_radiation": "solar_radiation_wm2",
            "relative_humidity_2m": "humidity_pct",
        }
    )
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame["city_id"] = city.id
    return frame


def test_reference_weights_are_positive_and_normalize():
    cities, metadata = load_city_reference()
    weights = pd.Series({city.id: city.population for city in cities}, dtype=float)
    weights /= weights.sum()
    assert len(cities) == 10
    assert metadata["population_reference_year"] == 2022
    assert weights.sum() == pytest.approx(1.0)
    assert weights["paris"] > weights["lyon"]


def test_fetch_uses_archive_utc_and_deterministic_cache(tmp_path):
    first = Session(payload())
    fetched = fetch_city_weather(
        PARIS, "2024-01-15", "2024-01-15", cache_dir=tmp_path,
        session=first, today=date(2024, 2, 1),
    )
    assert first.calls[0][0].endswith("/archive")
    assert first.calls[0][1]["params"]["timezone"] == "UTC"
    assert str(fetched["timestamp"].dt.tz) == "UTC"
    cached = fetch_city_weather(
        PARIS, "2024-01-15", "2024-01-15", cache_dir=tmp_path,
        session=Session({}, fail=True), today=date(2024, 2, 1),
    )
    pd.testing.assert_frame_equal(fetched, cached)
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_backward_alignment_has_no_future_leakage():
    aligned = align_city_weather(
        raw_city(PARIS, 10),
        pd.to_datetime(["2024-01-15T00:45Z", "2024-01-15T01:00Z"]),
    )
    assert aligned["temperature_c"].tolist() == [10.0, 11.0]
    assert (aligned["weather_source_timestamp"] <= aligned["timestamp"]).all()


def test_population_weighted_calculation_and_missing_city_coverage():
    weather = pd.concat([raw_city(PARIS, 10), raw_city(LYON, 40)], ignore_index=True)
    targets = pd.to_datetime(["2024-01-15T00:15Z"])
    full = build_national_weather_features(weather, targets, cities=[PARIS, LYON]).iloc[0]
    assert full["weather_temperature_c"] == pytest.approx(20.0)
    assert full["weather_population_coverage"] == pytest.approx(1.0)

    partial_aligned = align_city_weather(raw_city(PARIS, 10), targets)
    partial = population_weighted_features(partial_aligned, cities=[PARIS, LYON]).iloc[0]
    assert partial["weather_temperature_c"] == pytest.approx(10.0)
    assert partial["weather_population_coverage"] == pytest.approx(2 / 3)
    assert partial["weather_missing_cities"] == "lyon"


def test_utc_alignment_is_stable_across_dst_fallback():
    times = pd.date_range("2024-10-27T00:00Z", "2024-10-27T04:00Z", freq="1h")
    weather = raw_city(PARIS, 10).iloc[0:0].copy()
    weather = pd.DataFrame({
        "timestamp": times, "city_id": PARIS.id, "temperature_c": range(5),
        "wind_speed_kmh": 20.0, "cloud_cover_pct": 30.0,
        "solar_radiation_wm2": 40.0, "humidity_pct": 50.0,
    })
    targets = pd.date_range(times.min(), times.max(), freq="15min")
    aligned = align_city_weather(weather, targets)
    assert len(aligned) == 17
    assert aligned["timestamp"].is_unique
    assert str(aligned["timestamp"].dt.tz) == "UTC"


def test_energy_join_rejects_future_provenance():
    energy = pd.DataFrame({"timestamp": ["2024-01-15T00:15Z"], "consumption_mw": [50_000]})
    features = pd.DataFrame({
        "timestamp": ["2024-01-15T00:15Z"],
        "weather_source_timestamp_max": ["2024-01-15T00:00Z"],
        "weather_temperature_c": [10.0],
    })
    assert join_energy_weather(energy, features)["weather_temperature_c"].iloc[0] == 10
    features["weather_source_timestamp_max"] = "2024-01-15T00:30Z"
    with pytest.raises(ValueError, match="later than energy"):
        join_energy_weather(energy, features)
