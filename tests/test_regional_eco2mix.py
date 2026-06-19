from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import pandas as pd

from src.data_sources.rte_eco2mix_regional import (
    DATASET_ID,
    RegionalEco2MixError,
    demo_regional_snapshot,
    fallback_department_geojson,
    fallback_region_geojson,
    fetch_regional_eco2mix,
    normalize_region_geojson,
    prepare_regional_snapshot,
    region_code,
)


def regional_record(region: str = "Ile-de-France", consumption: int = 10_000) -> dict:
    return {
        "date_heure": "2026-06-17T10:00:00+00:00",
        "perimetre": region,
        "consommation": consumption,
        "nucleaire": 2000,
        "eolien": 300,
        "solaire": 200,
        "hydraulique": 100,
        "gaz": 400,
        "charbon": 0,
        "fioul": 0,
        "bioenergies": 100,
        "ech_physiques": 0,
        "taux_co2": 45,
    }


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class Session:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return Response(self.payload)


def test_fetch_regional_eco2mix_caches_raw_records(tmp_path: Path):
    session = Session({"total_count": 1, "results": [regional_record()]})
    frame = fetch_regional_eco2mix(
        datetime(2026, 6, 17, 9, tzinfo=timezone.utc),
        datetime(2026, 6, 17, 11, tzinfo=timezone.utc),
        cache_dir=tmp_path,
        session=session,
    )
    assert len(frame) == 1
    assert DATASET_ID in session.calls[0][0]
    assert list(tmp_path.glob("eco2mix_regional_*.json"))


def test_prepare_regional_snapshot_adds_codes_and_pressure():
    snapshot = prepare_regional_snapshot(
        frame := pd.DataFrame(
            [
                regional_record("Ile-de-France", 10_000),
                regional_record("Bretagne", 2_500),
            ]
        )
    )
    assert len(frame) == 2
    assert set(snapshot["region_code"]) == {"11", "53"}
    assert snapshot["demand_pressure"].max() == 1
    assert snapshot["national_demand_share"].sum() == pytest.approx(1)
    assert set(["regional_balance_mw", "demand_rank", "renewable_rank", "pressure_band"]).issubset(snapshot.columns)
    assert snapshot["region_display"].notna().all()


def test_demo_snapshot_joins_all_fallback_regions():
    snapshot = demo_regional_snapshot()
    geojson = fallback_region_geojson()
    geometry_codes = {feature["properties"]["code"] for feature in geojson["features"]}
    assert len(snapshot) == 13
    assert set(snapshot["region_code"]) == geometry_codes
    assert max(len(feature["geometry"]["coordinates"][0]) for feature in geojson["features"]) > 20


def test_department_boundary_fallback_is_available():
    geojson = fallback_department_geojson()
    assert geojson["type"] == "FeatureCollection"
    assert len(geojson["features"]) >= 90


def test_normalizes_accented_region_names_and_geojson():
    assert region_code("Provence-Alpes-Cote d'Azur") == "93"
    assert region_code("Bourgogne-Franche-Comte") == "27"
    geojson = normalize_region_geojson(
        {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"code": "11", "nom": "Ile-de-France"},
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[
                            [1.0, 48.0],
                            [2.0, 48.0],
                            [2.0, 49.0],
                            [1.0, 49.0],
                            [1.0, 48.0],
                        ]],
                    },
                }
            ],
        }
    )
    assert geojson["features"][0]["id"] == "11"
    assert geojson["features"][0]["properties"]["name"] == "Ile-de-France"


def test_normalizes_api_geo_list_response():
    geojson = normalize_region_geojson(
        [
            {
                "code": "53",
                "nom": "Bretagne",
                "contour": {
                    "type": "Polygon",
                    "coordinates": [[
                        [-5.0, 47.5],
                        [-1.0, 47.5],
                        [-1.0, 49.0],
                        [-5.0, 49.0],
                        [-5.0, 47.5],
                    ]],
                },
            }
        ]
    )
    assert geojson["type"] == "FeatureCollection"
    assert geojson["features"][0]["id"] == "53"
    assert geojson["features"][0]["geometry"]["type"] == "Polygon"


def test_api_geo_list_without_geometry_raises_regional_error():
    with pytest.raises(RegionalEco2MixError, match="no usable"):
        normalize_region_geojson([{"code": "53", "nom": "Bretagne"}])


def test_invalid_regional_response_is_explicit():
    session = Session({"total_count": 0, "results": []})
    with pytest.raises(RegionalEco2MixError, match="no populated records"):
        fetch_regional_eco2mix(
            datetime(2026, 6, 17, 9, tzinfo=timezone.utc),
            datetime(2026, 6, 17, 11, tzinfo=timezone.utc),
            cache=False,
            session=session,
        )
