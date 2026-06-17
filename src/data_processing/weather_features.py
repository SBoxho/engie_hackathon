"""Leakage-safe national weather features for 15-minute energy data."""
from __future__ import annotations

from collections.abc import Iterable

import pandas as pd

from src.data_sources.weather_national import City, load_city_reference

WEATHER_COLUMNS = (
    "temperature_c",
    "wind_speed_kmh",
    "cloud_cover_pct",
    "solar_radiation_wm2",
    "humidity_pct",
)


def _utc(values: Iterable) -> pd.DatetimeIndex:
    parsed = pd.DatetimeIndex(pd.to_datetime(values, utc=True, errors="raise"))
    return parsed


def align_city_weather(
    weather: pd.DataFrame,
    target_timestamps: Iterable,
    *,
    tolerance: str = "59min",
) -> pd.DataFrame:
    """Backward-only as-of alignment: a target can never see a later observation."""
    required = {"city_id", "timestamp", *WEATHER_COLUMNS}
    missing = required - set(weather.columns)
    if missing:
        raise ValueError(f"weather is missing columns: {sorted(missing)}")
    targets = pd.DataFrame({"timestamp": _utc(target_timestamps).unique()}).sort_values("timestamp")
    aligned: list[pd.DataFrame] = []
    for city_id, group in weather.groupby("city_id", sort=False):
        source = group.copy()
        source["timestamp"] = pd.to_datetime(source["timestamp"], utc=True, errors="raise")
        source = source.sort_values("timestamp").rename(columns={"timestamp": "weather_source_timestamp"})
        left = targets.copy()
        merged = pd.merge_asof(
            left.sort_values("timestamp"), source,
            left_on="timestamp", right_on="weather_source_timestamp",
            direction="backward", tolerance=pd.Timedelta(tolerance),
        )
        aligned.append(merged)
    result = pd.concat(aligned, ignore_index=True) if aligned else targets.iloc[0:0]
    valid = result["weather_source_timestamp"].notna()
    if (result.loc[valid, "weather_source_timestamp"] > result.loc[valid, "timestamp"]).any():
        raise AssertionError("future weather leaked into aligned features")
    return result.sort_values(["timestamp", "city_id"]).reset_index(drop=True)


def population_weighted_features(
    aligned_weather: pd.DataFrame,
    *,
    cities: Iterable[City] | None = None,
) -> pd.DataFrame:
    """Aggregate available cities and expose population coverage at every timestamp."""
    selected = list(cities) if cities is not None else load_city_reference()[0]
    populations = {city.id: city.population for city in selected}
    expected = set(populations)
    total_population = float(sum(populations.values()))
    rows: list[dict] = []
    for timestamp, group in aligned_weather.groupby("timestamp", sort=True):
        group = group[group["city_id"].isin(expected)].copy()
        available = group.loc[group[list(WEATHER_COLUMNS)].notna().all(axis=1), "city_id"].unique()
        available_set = set(available)
        available_population = float(sum(populations[city] for city in available_set))
        row = {
            "timestamp": timestamp,
            "weather_city_count": len(available_set),
            "weather_expected_city_count": len(expected),
            "weather_population_coverage": available_population / total_population,
            "weather_missing_cities": ",".join(sorted(expected - available_set)),
        }
        usable = group[group["city_id"].isin(available_set)]
        weights = usable["city_id"].map(populations).astype(float)
        for column in WEATHER_COLUMNS:
            row[f"weather_{column}"] = (
                (usable[column].astype(float) * weights).sum() / available_population
                if available_population else float("nan")
            )
        row["weather_source_timestamp_max"] = usable["weather_source_timestamp"].max()
        rows.append(row)
    return pd.DataFrame(rows)


def build_national_weather_features(
    weather: pd.DataFrame,
    target_timestamps: Iterable,
    *,
    cities: Iterable[City] | None = None,
) -> pd.DataFrame:
    aligned = align_city_weather(weather, target_timestamps)
    return population_weighted_features(aligned, cities=cities)


def join_energy_weather(energy: pd.DataFrame, weather_features: pd.DataFrame) -> pd.DataFrame:
    """Join exact UTC quarter-hours and reject provenance timestamps from the future."""
    if "timestamp" not in energy or "timestamp" not in weather_features:
        raise ValueError("energy and weather features require timestamp")
    left, right = energy.copy(), weather_features.copy()
    left["timestamp"] = pd.to_datetime(left["timestamp"], utc=True, errors="raise")
    right["timestamp"] = pd.to_datetime(right["timestamp"], utc=True, errors="raise")
    result = left.merge(right, on="timestamp", how="left", validate="many_to_one")
    source = pd.to_datetime(result.get("weather_source_timestamp_max"), utc=True, errors="coerce")
    valid = source.notna()
    if (source[valid] > result.loc[valid, "timestamp"]).any():
        raise ValueError("weather feature provenance is later than energy timestamp")
    return result
