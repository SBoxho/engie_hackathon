"""Weather-aware demand forecasting with explicit leakage controls."""
from __future__ import annotations

import hashlib
import json
import os
import pickle
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from src.models.demand_baselines import BASELINE_LAGS, HORIZON_HOURS, INTERVAL, _metrics


FEATURE_SCHEMA_VERSION = 1
MODEL_SCHEMA_VERSION = 1
EVALUATION_SCHEMA_VERSION = 1
DEFAULT_TIMEZONE = "Europe/Paris"
DEFAULT_RANDOM_SEED = 42
TARGET_COLUMN = "target_mw"
WEATHER_COLUMNS = (
    "weather_temperature_c",
    "weather_wind_speed_kmh",
    "weather_cloud_cover_pct",
    "weather_solar_radiation_wm2",
    "weather_humidity_pct",
)
WEATHER_META_COLUMNS = (
    "weather_population_coverage",
    "weather_city_count",
    "weather_expected_city_count",
    "weather_source_timestamp_max",
)
REQUIRED_ENERGY_COLUMNS = {"timestamp", "consumption_mw"}
MODEL_KIND = "sklearn.HistGradientBoostingRegressor"


@dataclass(frozen=True)
class FeatureConfig:
    timezone: str = DEFAULT_TIMEZONE
    horizons_hours: tuple[int, ...] = HORIZON_HOURS
    min_continuous_hours: float = 48.0
    cadence_minutes: int | None = None


@dataclass(frozen=True)
class TrainConfig:
    random_seed: int = DEFAULT_RANDOM_SEED
    test_fraction: float = 0.2
    validation_fraction: float = 0.2
    validation_folds: int = 3
    min_train_samples: int = 96
    min_test_samples: int = 24
    min_validation_samples: int = 24


def utc_iso(value: pd.Timestamp | datetime | None) -> str | None:
    if value is None or pd.isna(value):
        return None
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC").isoformat().replace("+00:00", "Z")


def generated_at_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def dataframe_digest(frame: pd.DataFrame) -> str:
    if frame.empty:
        return "empty"
    hashed = pd.util.hash_pandas_object(frame.reset_index(drop=True), index=True).values
    return hashlib.sha256(hashed.tobytes()).hexdigest()


def _json_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    serializable = frame.copy()
    for column in serializable.select_dtypes(include=["datetimetz", "datetime"]).columns:
        serializable[column] = serializable[column].map(utc_iso)
    serializable = serializable.replace({np.nan: None})
    return json.loads(serializable.to_json(orient="records", double_precision=10))


def _normalize_timestamp_column(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    if "timestamp" not in result:
        raise ValueError("Demand data requires a timestamp column.")
    result["timestamp"] = pd.to_datetime(result["timestamp"], utc=True, errors="coerce")
    if result["timestamp"].isna().any():
        raise ValueError("Demand data contains invalid or missing timestamps.")
    return result


def inspect_demand_dataset(
    energy: pd.DataFrame,
    *,
    weather: pd.DataFrame | None = None,
    timezone_name: str = DEFAULT_TIMEZONE,
    cadence_minutes: int | None = None,
    min_continuous_hours: float = 48.0,
) -> dict[str, Any]:
    """Return coverage, cadence, duplicate, schema, and weather diagnostics."""
    audit: dict[str, Any] = {
        "schema_version": FEATURE_SCHEMA_VERSION,
        "timezone": timezone_name,
        "row_count": int(len(energy)),
        "required_columns": sorted(REQUIRED_ENERGY_COLUMNS),
        "missing_required_columns": sorted(REQUIRED_ENERGY_COLUMNS - set(energy.columns)),
        "extra_columns": sorted(set(energy.columns) - REQUIRED_ENERGY_COLUMNS),
    }
    if not REQUIRED_ENERGY_COLUMNS.issubset(energy.columns) or energy.empty:
        audit.update(
            {
                "start_utc": None,
                "end_utc": None,
                "cadence_minutes": 15,
                "expected_interval_count": 0,
                "missing_interval_count": 0,
                "duplicate_timestamp_count": 0,
                "missing_target_count": 0,
                "continuous_periods": [],
                "eligible_continuous_periods": [],
                "weather": None,
            }
        )
        return audit

    frame = _normalize_timestamp_column(energy)
    frame = frame.sort_values("timestamp", kind="stable")
    cadence = _cadence_delta(frame, cadence_minutes)
    timestamps = frame["timestamp"]
    duplicate_count = int(timestamps.duplicated().sum())
    unique_timestamps = pd.DatetimeIndex(timestamps.drop_duplicates().sort_values())
    start = unique_timestamps.min()
    end = unique_timestamps.max()
    expected = pd.date_range(start, end, freq=cadence, tz="UTC")
    missing = expected.difference(unique_timestamps)
    diffs = unique_timestamps.to_series().diff().dropna()
    cadence_mode = diffs.mode().iloc[0] if not diffs.empty else pd.NaT
    off_grid = (
        timestamps.dt.minute.mod(15).ne(0)
        | timestamps.dt.second.ne(0)
        | timestamps.dt.microsecond.ne(0)
    )

    periods = continuous_periods(frame, cadence=cadence)
    eligible = [
        period
        for period in periods
        if period["duration_hours"] >= min_continuous_hours
    ]
    audit.update(
        {
            "start_utc": utc_iso(start),
            "end_utc": utc_iso(end),
            "cadence_minutes": int(cadence / pd.Timedelta(minutes=1)),
            "observed_cadence_mode_minutes": (
                None if pd.isna(cadence_mode) else cadence_mode.total_seconds() / 60
            ),
            "expected_interval_count": int(len(expected)),
            "missing_interval_count": int(len(missing)),
            "duplicate_timestamp_count": duplicate_count,
            "off_grid_timestamp_count": int(off_grid.sum()),
            "missing_target_count": int(pd.to_numeric(frame["consumption_mw"], errors="coerce").isna().sum()),
            "continuous_periods": periods,
            "eligible_continuous_periods": eligible,
        }
    )
    if weather is not None and not weather.empty and "timestamp" in weather:
        weather_frame = _normalize_timestamp_column(weather)
        in_range = weather_frame[
            weather_frame["timestamp"].between(start, end, inclusive="both")
        ].copy()
        if "weather_population_coverage" in in_range:
            coverage = pd.to_numeric(in_range["weather_population_coverage"], errors="coerce")
        else:
            coverage = pd.Series(dtype=float)
        audit["weather"] = {
            "row_count": int(len(weather_frame)),
            "start_utc": utc_iso(weather_frame["timestamp"].min()),
            "end_utc": utc_iso(weather_frame["timestamp"].max()),
            "overlap_row_count": int(len(in_range)),
            "overlap_fraction_of_energy_timestamps": (
                float(len(set(in_range["timestamp"]).intersection(set(unique_timestamps))) / len(unique_timestamps))
                if len(unique_timestamps)
                else 0.0
            ),
            "mean_population_coverage": float(coverage.mean()) if not coverage.empty else None,
            "min_population_coverage": float(coverage.min()) if not coverage.empty else None,
            "missing_weather_feature_rows": int(in_range[list(set(WEATHER_COLUMNS) & set(in_range.columns))].isna().any(axis=1).sum())
            if set(WEATHER_COLUMNS) & set(in_range.columns)
            else int(len(in_range)),
        }
    else:
        audit["weather"] = {
            "row_count": 0,
            "overlap_row_count": 0,
            "overlap_fraction_of_energy_timestamps": 0.0,
            "mean_population_coverage": None,
            "min_population_coverage": None,
            "missing_weather_feature_rows": None,
        }
    return audit


def _cadence_delta(frame: pd.DataFrame, cadence_minutes: int | None = None) -> pd.Timedelta:
    if cadence_minutes is not None:
        if cadence_minutes <= 0:
            raise ValueError("cadence_minutes must be positive")
        return pd.Timedelta(minutes=int(cadence_minutes))
    if frame.empty or "timestamp" not in frame:
        return INTERVAL
    timestamps = pd.DatetimeIndex(pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")).dropna()
    unique = timestamps.drop_duplicates().sort_values()
    diffs = unique.to_series().diff().dropna()
    diffs = diffs[diffs > pd.Timedelta(0)]
    if diffs.empty:
        return INTERVAL
    return pd.Timedelta(diffs.mode().iloc[0])


def continuous_periods(frame: pd.DataFrame, *, cadence: pd.Timedelta = INTERVAL) -> list[dict[str, Any]]:
    """Find exact observed-cadence periods with non-missing demand."""
    if frame.empty:
        return []
    data = _normalize_timestamp_column(frame)
    data["consumption_mw"] = pd.to_numeric(data["consumption_mw"], errors="coerce")
    data = data.sort_values("timestamp", kind="stable").drop_duplicates("timestamp", keep="last")
    rows: list[dict[str, Any]] = []
    current_start: pd.Timestamp | None = None
    current_end: pd.Timestamp | None = None
    current_rows = 0
    previous: pd.Timestamp | None = None
    for row in data.itertuples(index=False):
        timestamp = row.timestamp
        valid = pd.notna(row.consumption_mw)
        contiguous = previous is not None and timestamp - previous == cadence
        if not valid:
            if current_start is not None and current_end is not None:
                rows.append(_period_row(current_start, current_end, current_rows, cadence))
            current_start = current_end = previous = None
            current_rows = 0
            continue
        if current_start is None or not contiguous:
            if current_start is not None and current_end is not None:
                rows.append(_period_row(current_start, current_end, current_rows, cadence))
            current_start = timestamp
            current_rows = 1
        else:
            current_rows += 1
        current_end = timestamp
        previous = timestamp
    if current_start is not None and current_end is not None:
        rows.append(_period_row(current_start, current_end, current_rows, cadence))
    return rows


def _period_row(start: pd.Timestamp, end: pd.Timestamp, rows: int, cadence: pd.Timedelta) -> dict[str, Any]:
    return {
        "start_utc": utc_iso(start),
        "end_utc": utc_iso(end),
        "row_count": int(rows),
        "duration_hours": float((end - start + cadence) / pd.Timedelta(hours=1)),
    }


def prepare_model_input(energy: pd.DataFrame, weather: pd.DataFrame | None = None) -> pd.DataFrame:
    """Join weather exactly at origin timestamps and verify source provenance."""
    if not REQUIRED_ENERGY_COLUMNS.issubset(energy.columns):
        raise ValueError(f"Energy data is missing required columns: {sorted(REQUIRED_ENERGY_COLUMNS - set(energy.columns))}")
    result = _normalize_timestamp_column(energy)
    result["consumption_mw"] = pd.to_numeric(result["consumption_mw"], errors="coerce")
    if result["timestamp"].duplicated().any():
        raise ValueError("Demand timestamps must be unique before feature generation.")
    off_grid = (
        result["timestamp"].dt.minute.mod(15).ne(0)
        | result["timestamp"].dt.second.ne(0)
        | result["timestamp"].dt.microsecond.ne(0)
    )
    if off_grid.any():
        raise ValueError("Demand timestamps must lie on an exact 15-minute UTC grid.")
    if weather is not None and not weather.empty:
        weather_frame = _normalize_timestamp_column(weather)
        keep_columns = [
            column
            for column in ("timestamp", *WEATHER_COLUMNS, *WEATHER_META_COLUMNS, "weather_missing_cities")
            if column in weather_frame.columns
        ]
        weather_frame = weather_frame[keep_columns].drop_duplicates("timestamp", keep="last")
        result = result.merge(weather_frame, on="timestamp", how="left", validate="one_to_one")
        if "weather_source_timestamp_max" in result:
            source = pd.to_datetime(result["weather_source_timestamp_max"], utc=True, errors="coerce")
            valid = source.notna()
            if (source[valid] > result.loc[valid, "timestamp"]).any():
                raise ValueError("Weather feature provenance is later than the forecast origin.")
    for column in WEATHER_COLUMNS:
        if column not in result:
            result[column] = np.nan
    if "weather_population_coverage" not in result:
        result["weather_population_coverage"] = 0.0
    return result.sort_values("timestamp", kind="stable").reset_index(drop=True)


def build_feature_frame(
    energy: pd.DataFrame,
    *,
    weather: pd.DataFrame | None = None,
    config: FeatureConfig | None = None,
    source: str = "unknown",
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Create one supervised row per origin and direct horizon."""
    config = config or FeatureConfig()
    model_input = prepare_model_input(energy, weather)
    cadence = _cadence_delta(model_input, config.cadence_minutes)
    audit = inspect_demand_dataset(
        energy,
        weather=weather,
        timezone_name=config.timezone,
        cadence_minutes=config.cadence_minutes,
        min_continuous_hours=config.min_continuous_hours,
    )
    periods = continuous_periods(model_input, cadence=cadence)
    eligible_periods = [
        period for period in periods if period["duration_hours"] >= config.min_continuous_hours
    ]
    if not eligible_periods:
        raise ValueError(
            "No sufficiently continuous non-missing demand period is available for training. "
            f"Need at least {config.min_continuous_hours:g} hours."
        )

    base = _base_features(model_input, config.timezone, cadence)
    values = model_input.set_index("timestamp")["consumption_mw"].sort_index()
    block_by_timestamp = _continuous_block_ids(model_input, cadence=cadence)
    feature_parts: list[pd.DataFrame] = []
    for horizon in config.horizons_hours:
        horizon_delta = pd.Timedelta(hours=int(horizon))
        horizon_frame = base.copy()
        horizon_frame["horizon_hours"] = int(horizon)
        horizon_frame["target_timestamp"] = horizon_frame["origin_timestamp"] + horizon_delta
        horizon_frame[TARGET_COLUMN] = values.reindex(horizon_frame["target_timestamp"]).to_numpy()
        horizon_frame["origin_block_id"] = horizon_frame["origin_timestamp"].map(block_by_timestamp)
        horizon_frame["target_block_id"] = horizon_frame["target_timestamp"].map(block_by_timestamp)
        horizon_frame["same_continuous_block"] = (
            horizon_frame["origin_block_id"].notna()
            & horizon_frame["origin_block_id"].eq(horizon_frame["target_block_id"])
        )
        horizon_frame["eligible_continuous_period"] = horizon_frame["origin_timestamp"].map(
            _eligible_timestamp_lookup(eligible_periods, cadence=cadence)
        ).eq(True)
        feature_parts.append(horizon_frame)
    features = pd.concat(feature_parts, ignore_index=True)
    features = features.sort_values(["horizon_hours", "origin_timestamp"], kind="stable").reset_index(drop=True)
    features = add_target_calendar_features(features, config.timezone)
    feature_columns = model_feature_columns(features)
    metadata = {
        "schema_version": FEATURE_SCHEMA_VERSION,
        "generated_at": generated_at_utc(),
        "source": source,
        "feature_config": {
            **asdict(config),
            "resolved_cadence_minutes": int(cadence / pd.Timedelta(minutes=1)),
        },
        "target_column": TARGET_COLUMN,
        "feature_columns": feature_columns,
        "weather_columns": list(WEATHER_COLUMNS),
        "leakage_controls": [
            "target demand is joined only at exact target timestamps",
            "demand lag source timestamps are less than or equal to the forecast origin",
            "rolling demand statistics are shifted by one interval before aggregation",
            "weather source timestamps must be less than or equal to the forecast origin",
            "target calendar features are deterministic calendar values, not observed future data",
        ],
        "audit": {**audit, "eligible_continuous_periods": eligible_periods},
        "row_count": int(len(features)),
        "usable_target_rows": int(features[TARGET_COLUMN].notna().sum()),
        "data_digest": dataframe_digest(features[["origin_timestamp", "target_timestamp", "horizon_hours", TARGET_COLUMN]]),
    }
    validate_no_leakage(features)
    return features, metadata


def _base_features(frame: pd.DataFrame, timezone_name: str, cadence: pd.Timedelta) -> pd.DataFrame:
    timestamps = pd.DatetimeIndex(frame["timestamp"])
    local_origin = pd.Series(timestamps.tz_convert(timezone_name), index=frame.index)
    values = frame.set_index("timestamp")["consumption_mw"].sort_index()
    full_grid = pd.date_range(timestamps.min(), timestamps.max(), freq=cadence, tz="UTC")
    grid_values = values.reindex(full_grid)
    shifted = grid_values.shift(1)
    result = pd.DataFrame({"origin_timestamp": timestamps})
    result["origin_demand_mw"] = values.reindex(timestamps).to_numpy()
    for hours in (1, 3, 6, 24, 168):
        source_times = timestamps - pd.Timedelta(hours=hours)
        result[f"demand_lag_{hours}h_mw"] = values.reindex(source_times).to_numpy()
    for hours in (1, 4, 24):
        window = max(1, int(pd.Timedelta(hours=hours) / cadence))
        rolling = shifted.rolling(window=window, min_periods=window)
        result[f"demand_roll_{hours}h_mean_mw"] = rolling.mean().reindex(timestamps).to_numpy()
        result[f"demand_roll_{hours}h_std_mw"] = rolling.std(ddof=0).reindex(timestamps).to_numpy()
    result["origin_hour"] = local_origin.dt.hour.astype(int)
    result["origin_weekday"] = local_origin.dt.dayofweek.astype(int)
    result["origin_month"] = local_origin.dt.month.astype(int)
    result["origin_dayofyear"] = local_origin.dt.dayofyear.astype(int)
    result["origin_is_weekend"] = result["origin_weekday"].ge(5).astype(int)
    result["origin_season"] = result["origin_month"].map(_season_number).astype(int)
    result["origin_is_dst"] = local_origin.map(lambda value: int(bool(value.dst()))).astype(int)
    result["origin_utc_offset_hours"] = local_origin.map(
        lambda value: value.utcoffset().total_seconds() / 3600
    ).astype(float)
    result["origin_hour_sin"] = np.sin(2 * np.pi * result["origin_hour"] / 24)
    result["origin_hour_cos"] = np.cos(2 * np.pi * result["origin_hour"] / 24)
    result["origin_weekday_sin"] = np.sin(2 * np.pi * result["origin_weekday"] / 7)
    result["origin_weekday_cos"] = np.cos(2 * np.pi * result["origin_weekday"] / 7)
    result["origin_is_holiday"] = _holiday_flags(local_origin).astype(int)
    target_placeholders = pd.DataFrame(index=result.index)
    for column in WEATHER_COLUMNS:
        result[column] = pd.to_numeric(frame[column], errors="coerce") if column in frame else np.nan
        result[f"{column}_missing"] = result[column].isna().astype(int)
    result["weather_population_coverage"] = pd.to_numeric(
        frame.get("weather_population_coverage", 0.0), errors="coerce"
    ).fillna(0.0)
    result["weather_missing_count"] = result[[f"{column}_missing" for column in WEATHER_COLUMNS]].sum(axis=1)
    if "weather_city_count" in frame and "weather_expected_city_count" in frame:
        expected = pd.to_numeric(frame["weather_expected_city_count"], errors="coerce")
        actual = pd.to_numeric(frame["weather_city_count"], errors="coerce")
        result["weather_missing_city_count"] = (expected - actual).clip(lower=0).fillna(expected).fillna(0)
    else:
        result["weather_missing_city_count"] = 0.0
    if "weather_source_timestamp_max" in frame:
        source = pd.to_datetime(frame["weather_source_timestamp_max"], utc=True, errors="coerce")
        result["weather_source_age_minutes"] = (
            (timestamps.to_series(index=result.index) - source) / pd.Timedelta(minutes=1)
        )
    else:
        result["weather_source_age_minutes"] = np.nan
    return pd.concat([result, target_placeholders], axis=1)


def add_target_calendar_features(features: pd.DataFrame, timezone_name: str = DEFAULT_TIMEZONE) -> pd.DataFrame:
    result = features.copy()
    local_target = pd.Series(
        pd.DatetimeIndex(result["target_timestamp"]).tz_convert(timezone_name), index=result.index
    )
    result["target_hour"] = local_target.dt.hour.astype(int)
    result["target_weekday"] = local_target.dt.dayofweek.astype(int)
    result["target_month"] = local_target.dt.month.astype(int)
    result["target_season"] = result["target_month"].map(_season_number).astype(int)
    result["target_is_weekend"] = result["target_weekday"].ge(5).astype(int)
    result["target_is_dst"] = local_target.map(lambda value: int(bool(value.dst()))).astype(int)
    result["target_utc_offset_hours"] = local_target.map(
        lambda value: value.utcoffset().total_seconds() / 3600
    ).astype(float)
    result["target_hour_sin"] = np.sin(2 * np.pi * result["target_hour"] / 24)
    result["target_hour_cos"] = np.cos(2 * np.pi * result["target_hour"] / 24)
    result["target_weekday_sin"] = np.sin(2 * np.pi * result["target_weekday"] / 7)
    result["target_weekday_cos"] = np.cos(2 * np.pi * result["target_weekday"] / 7)
    result["target_is_holiday"] = _holiday_flags(local_target).astype(int)
    return result


def _season_number(month: int) -> int:
    if month in (12, 1, 2):
        return 0
    if month in (3, 4, 5):
        return 1
    if month in (6, 7, 8):
        return 2
    return 3


def _holiday_flags(local_times: pd.Series) -> pd.Series:
    try:
        import holidays

        years = sorted({int(value.year) for value in local_times})
        calendar = holidays.France(years=years)
        return local_times.dt.date.map(lambda day: day in calendar)
    except ImportError:
        return pd.Series(False, index=local_times.index)


def _continuous_block_ids(frame: pd.DataFrame, *, cadence: pd.Timedelta = INTERVAL) -> dict[pd.Timestamp, int]:
    data = frame[["timestamp", "consumption_mw"]].copy()
    data["consumption_mw"] = pd.to_numeric(data["consumption_mw"], errors="coerce")
    data = data.sort_values("timestamp", kind="stable")
    result: dict[pd.Timestamp, int] = {}
    block_id = -1
    previous: pd.Timestamp | None = None
    for row in data.itertuples(index=False):
        timestamp = row.timestamp
        if pd.isna(row.consumption_mw):
            previous = None
            continue
        if previous is None or timestamp - previous != cadence:
            block_id += 1
        result[timestamp] = block_id
        previous = timestamp
    return result


def _eligible_timestamp_lookup(
    periods: list[dict[str, Any]], *, cadence: pd.Timedelta = INTERVAL
) -> dict[pd.Timestamp, bool]:
    lookup: dict[pd.Timestamp, bool] = {}
    for period in periods:
        start = pd.Timestamp(period["start_utc"])
        end = pd.Timestamp(period["end_utc"])
        for timestamp in pd.date_range(start, end, freq=cadence, tz="UTC"):
            lookup[timestamp] = True
    return lookup


def model_feature_columns(features: pd.DataFrame) -> list[str]:
    excluded = {
        "origin_timestamp",
        "target_timestamp",
        TARGET_COLUMN,
        "origin_block_id",
        "target_block_id",
        "same_continuous_block",
        "eligible_continuous_period",
    }
    timestamp_like = {
        column for column in features.columns if pd.api.types.is_datetime64_any_dtype(features[column])
    }
    return [
        column
        for column in features.columns
        if column not in excluded and column not in timestamp_like
    ]


def validate_no_leakage(features: pd.DataFrame) -> None:
    required = {"origin_timestamp", "target_timestamp", "horizon_hours"}
    missing = required - set(features.columns)
    if missing:
        raise ValueError(f"Feature frame missing leakage-control columns: {sorted(missing)}")
    origin = pd.to_datetime(features["origin_timestamp"], utc=True)
    target = pd.to_datetime(features["target_timestamp"], utc=True)
    expected_target = origin + pd.to_timedelta(features["horizon_hours"], unit="h")
    if not target.eq(expected_target).all():
        raise ValueError("Target timestamps are not exactly aligned to the requested horizons.")
    if target.le(origin).any():
        raise ValueError("Target timestamps must be later than forecast origins.")
    if "weather_source_age_minutes" in features:
        ages = pd.to_numeric(features["weather_source_age_minutes"], errors="coerce").dropna()
        if ages.lt(0).any():
            raise ValueError("Weather source timestamps are later than forecast origins.")


def valid_supervised_rows(features: pd.DataFrame, horizon: int) -> pd.DataFrame:
    subset = features.loc[features["horizon_hours"].eq(int(horizon))].copy()
    subset = subset[
        subset[TARGET_COLUMN].notna()
        & subset["same_continuous_block"].astype(bool)
        & subset["eligible_continuous_period"].astype(bool)
    ]
    return subset.sort_values("target_timestamp", kind="stable").reset_index(drop=True)


def chronological_split(
    rows: pd.DataFrame,
    *,
    config: TrainConfig,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    sample_count = len(rows)
    test_count = max(config.min_test_samples, int(np.ceil(sample_count * config.test_fraction)))
    if sample_count - test_count < config.min_train_samples:
        raise ValueError(
            f"Need at least {config.min_train_samples} train and {config.min_test_samples} test "
            f"samples; only {sample_count} supervised samples are available."
        )
    return rows.iloc[: sample_count - test_count].copy(), rows.iloc[sample_count - test_count :].copy()


def expanding_validation_splits(
    train_rows: pd.DataFrame,
    *,
    config: TrainConfig,
) -> list[tuple[pd.DataFrame, pd.DataFrame]]:
    rows = train_rows.sort_values("target_timestamp", kind="stable").reset_index(drop=True)
    if len(rows) < config.min_train_samples + config.min_validation_samples:
        return []
    fold_count = max(1, config.validation_folds)
    validation_size = max(
        config.min_validation_samples,
        int(np.floor(len(rows) * config.validation_fraction / fold_count)),
    )
    splits: list[tuple[pd.DataFrame, pd.DataFrame]] = []
    for fold in range(fold_count, 0, -1):
        valid_end = len(rows) - (fold - 1) * validation_size
        valid_start = valid_end - validation_size
        if valid_start < config.min_train_samples:
            continue
        splits.append((rows.iloc[:valid_start].copy(), rows.iloc[valid_start:valid_end].copy()))
    return splits


def train_models(
    features: pd.DataFrame,
    metadata: dict[str, Any],
    *,
    config: TrainConfig | None = None,
) -> dict[str, Any]:
    """Train one deterministic direct model per eligible horizon."""
    config = config or TrainConfig()
    validate_feature_metadata(features, metadata)
    os.environ.setdefault("LOKY_MAX_CPU_COUNT", "1")
    try:
        from sklearn.ensemble import HistGradientBoostingRegressor
    except ImportError as exc:
        raise RuntimeError("scikit-learn is required to train the demand model.") from exc

    feature_columns = list(metadata["feature_columns"])
    models: dict[int, Any] = {}
    feature_columns_by_horizon: dict[str, list[str]] = {}
    horizon_metadata: dict[str, Any] = {}
    validation_rows: list[dict[str, Any]] = []
    skipped: dict[str, str] = {}
    for horizon in metadata["feature_config"]["horizons_hours"]:
        horizon = int(horizon)
        rows = add_target_calendar_features(valid_supervised_rows(features, horizon), metadata["feature_config"]["timezone"])
        if rows.empty:
            skipped[str(horizon)] = "no valid exact target rows in sufficiently continuous periods"
            continue
        try:
            train_rows, test_rows = chronological_split(rows, config=config)
        except ValueError as exc:
            skipped[str(horizon)] = str(exc)
            continue
        horizon_feature_columns = _informative_feature_columns(train_rows, feature_columns)
        if not horizon_feature_columns:
            skipped[str(horizon)] = "no informative non-leakage feature columns are available"
            continue
        for fold_index, (fold_train, fold_valid) in enumerate(expanding_validation_splits(train_rows, config=config), start=1):
            fold_feature_columns = _informative_feature_columns(fold_train, horizon_feature_columns)
            if not fold_feature_columns:
                continue
            fold_model = HistGradientBoostingRegressor(
                loss="squared_error",
                learning_rate=0.05,
                max_iter=80,
                max_leaf_nodes=31,
                l2_regularization=0.05,
                early_stopping=False,
                random_state=config.random_seed,
            )
            fold_model.fit(fold_train[fold_feature_columns], fold_train[TARGET_COLUMN])
            predicted = fold_model.predict(fold_valid[fold_feature_columns])
            metrics = regression_metrics(fold_valid[TARGET_COLUMN].to_numpy(), predicted)
            validation_rows.append({"horizon_hours": horizon, "fold": fold_index, **metrics})
        model = HistGradientBoostingRegressor(
            loss="squared_error",
            learning_rate=0.05,
            max_iter=80,
            max_leaf_nodes=31,
            l2_regularization=0.05,
            early_stopping=False,
            random_state=config.random_seed,
        )
        model.fit(train_rows[horizon_feature_columns], train_rows[TARGET_COLUMN])
        models[horizon] = model
        feature_columns_by_horizon[str(horizon)] = horizon_feature_columns
        horizon_metadata[str(horizon)] = {
            "train_start_utc": utc_iso(train_rows["target_timestamp"].min()),
            "train_end_utc": utc_iso(train_rows["target_timestamp"].max()),
            "test_start_utc": utc_iso(test_rows["target_timestamp"].min()),
            "test_end_utc": utc_iso(test_rows["target_timestamp"].max()),
            "train_samples": int(len(train_rows)),
            "test_samples": int(len(test_rows)),
        }
    if not models:
        raise ValueError(f"No horizon had enough supervised samples to train. Skipped: {skipped}")
    return {
        "schema_version": MODEL_SCHEMA_VERSION,
        "model_kind": MODEL_KIND,
        "generated_at": generated_at_utc(),
        "feature_metadata": metadata,
        "train_config": asdict(config),
        "feature_columns": feature_columns,
        "feature_columns_by_horizon": feature_columns_by_horizon,
        "models": models,
        "horizons": sorted(models),
        "horizon_metadata": horizon_metadata,
        "validation_metrics": validation_rows,
        "skipped_horizons": skipped,
    }


def regression_metrics(actual: Iterable[float], predicted: Iterable[float]) -> dict[str, Any]:
    actual_array = np.asarray(list(actual), dtype=float)
    predicted_array = np.asarray(list(predicted), dtype=float)
    valid = ~(np.isnan(actual_array) | np.isnan(predicted_array))
    if not valid.any():
        return {
            "mae_mw": None,
            "rmse_mw": None,
            "smape_percent": None,
            "sample_count": 0,
            "coverage": 0.0,
        }
    actual_valid = actual_array[valid]
    predicted_valid = predicted_array[valid]
    error = actual_valid - predicted_valid
    denominator = np.abs(actual_valid) + np.abs(predicted_valid)
    smape = np.divide(200 * np.abs(error), denominator, out=np.zeros_like(error), where=denominator != 0)
    return {
        "mae_mw": float(np.mean(np.abs(error))),
        "rmse_mw": float(np.sqrt(np.mean(error**2))),
        "smape_percent": float(np.mean(smape)),
        "sample_count": int(valid.sum()),
        "coverage": float(valid.sum() / len(actual_array)) if len(actual_array) else 0.0,
    }


def _informative_feature_columns(rows: pd.DataFrame, feature_columns: list[str]) -> list[str]:
    informative: list[str] = []
    for column in feature_columns:
        values = pd.to_numeric(rows[column], errors="coerce")
        if values.dropna().nunique() >= 2:
            informative.append(column)
    return informative


def evaluate_models(
    features: pd.DataFrame,
    model_bundle: dict[str, Any],
    *,
    min_segment_samples: int = 24,
) -> dict[str, Any]:
    validate_model_bundle(model_bundle)
    metadata = model_bundle["feature_metadata"]
    validate_feature_metadata(features, metadata)
    feature_columns = list(model_bundle["feature_columns"])
    prediction_parts: list[pd.DataFrame] = []
    metric_rows: list[dict[str, Any]] = []
    comparison_rows: list[dict[str, Any]] = []
    segment_rows: list[dict[str, Any]] = []

    for horizon in model_bundle["horizons"]:
        horizon = int(horizon)
        rows = add_target_calendar_features(valid_supervised_rows(features, horizon), metadata["feature_config"]["timezone"])
        train_config = TrainConfig(**model_bundle["train_config"])
        _, test_rows = chronological_split(rows, config=train_config)
        model = model_bundle["models"][horizon]
        horizon_feature_columns = model_bundle.get("feature_columns_by_horizon", {}).get(str(horizon), feature_columns)
        predicted = model.predict(test_rows[horizon_feature_columns])
        predictions = test_rows[
            ["origin_timestamp", "target_timestamp", "horizon_hours", TARGET_COLUMN, "target_hour", "target_season"]
        ].copy()
        predictions["model_predicted_mw"] = predicted
        predictions = add_baseline_predictions(predictions, features)
        prediction_parts.append(predictions)

        model_metrics = regression_metrics(predictions[TARGET_COLUMN], predictions["model_predicted_mw"])
        metric_rows.append({"model": "demand_hgb", "horizon_hours": horizon, **model_metrics})
        baseline_metric_rows: list[dict[str, Any]] = []
        for baseline in BASELINE_LAGS:
            metrics = regression_metrics(predictions[TARGET_COLUMN], predictions[f"{baseline}_predicted_mw"])
            row = {"model": baseline, "horizon_hours": horizon, **metrics}
            metric_rows.append(row)
            if metrics["sample_count"] and metrics["mae_mw"] is not None:
                baseline_metric_rows.append(row)
        strongest = min(baseline_metric_rows, key=lambda row: row["mae_mw"]) if baseline_metric_rows else None
        comparison_rows.append(
            {
                "horizon_hours": horizon,
                "model_mae_mw": model_metrics["mae_mw"],
                "strongest_baseline": strongest["model"] if strongest else None,
                "strongest_baseline_mae_mw": strongest["mae_mw"] if strongest else None,
                "improvement_vs_strongest_baseline_percent": (
                    100.0 * (strongest["mae_mw"] - model_metrics["mae_mw"]) / strongest["mae_mw"]
                    if strongest and strongest["mae_mw"]
                    else None
                ),
                "baseline_eligible_count": len(baseline_metric_rows),
            }
        )
        segment_rows.extend(_segment_metrics(predictions, horizon, min_segment_samples))

    all_predictions = (
        pd.concat(prediction_parts, ignore_index=True)
        if prediction_parts
        else pd.DataFrame()
    )
    return {
        "schema_version": EVALUATION_SCHEMA_VERSION,
        "generated_at": generated_at_utc(),
        "model_schema_version": model_bundle["schema_version"],
        "feature_schema_version": metadata["schema_version"],
        "model_kind": model_bundle["model_kind"],
        "source": metadata.get("source"),
        "feature_data_digest": metadata.get("data_digest"),
        "training_periods": model_bundle["horizon_metadata"],
        "data_audit": metadata.get("audit", {}),
        "metrics": metric_rows,
        "baseline_comparison": comparison_rows,
        "segment_metrics": segment_rows,
        "predictions": _json_records(all_predictions),
        "skipped_horizons": model_bundle.get("skipped_horizons", {}),
        "disclaimer": "Experimental weather-aware model; not an RTE operational forecast.",
    }


def add_baseline_predictions(predictions: pd.DataFrame, features: pd.DataFrame) -> pd.DataFrame:
    result = predictions.copy()
    values = (
        features[["origin_timestamp", "origin_demand_mw"]]
        .drop_duplicates("origin_timestamp", keep="last")
        .set_index("origin_timestamp")["origin_demand_mw"]
        .sort_index()
    )
    origin = pd.DatetimeIndex(result["origin_timestamp"])
    target = pd.DatetimeIndex(result["target_timestamp"])
    for baseline, lag in BASELINE_LAGS.items():
        source_times = origin if lag is None else target - lag
        if (source_times > origin).any():
            raise ValueError(f"{baseline} would use observations after the forecast origin.")
        result[f"{baseline}_source_timestamp"] = source_times
        result[f"{baseline}_predicted_mw"] = values.reindex(source_times).to_numpy()
    return result


def _segment_metrics(predictions: pd.DataFrame, horizon: int, min_segment_samples: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for segment_name, column in (("target_hour", "target_hour"), ("target_season", "target_season")):
        for value, group in predictions.groupby(column, sort=True):
            if len(group) < min_segment_samples:
                continue
            metrics = regression_metrics(group[TARGET_COLUMN], group["model_predicted_mw"])
            rows.append(
                {
                    "horizon_hours": horizon,
                    "segment": segment_name,
                    "segment_value": int(value),
                    **metrics,
                }
            )
    return rows


def validate_feature_metadata(features: pd.DataFrame, metadata: dict[str, Any]) -> None:
    if metadata.get("schema_version") != FEATURE_SCHEMA_VERSION:
        raise ValueError("Unsupported feature schema version.")
    missing = set(metadata.get("feature_columns", [])).difference(features.columns)
    if missing:
        raise ValueError(f"Feature frame is missing model feature columns: {sorted(missing)}")
    validate_no_leakage(features)


def validate_model_bundle(bundle: dict[str, Any]) -> None:
    if bundle.get("schema_version") != MODEL_SCHEMA_VERSION:
        raise ValueError("Unsupported model artifact schema version.")
    if bundle.get("model_kind") != MODEL_KIND:
        raise ValueError("Unsupported model kind.")
    metadata = bundle.get("feature_metadata")
    if not isinstance(metadata, dict):
        raise ValueError("Model artifact is missing feature metadata.")
    for horizon in bundle.get("horizons", []):
        if int(horizon) not in bundle.get("models", {}):
            raise ValueError(f"Model artifact is missing horizon {horizon}.")
        if str(int(horizon)) not in bundle.get("feature_columns_by_horizon", {}):
            raise ValueError(f"Model artifact is missing feature schema for horizon {horizon}.")


def save_feature_metadata(metadata: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(metadata, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


def load_feature_metadata(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def save_model_bundle(bundle: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        pickle.dump(bundle, handle, protocol=pickle.HIGHEST_PROTOCOL)


def load_model_bundle(path: Path) -> dict[str, Any]:
    os.environ.setdefault("LOKY_MAX_CPU_COUNT", "1")
    with path.open("rb") as handle:
        bundle = pickle.load(handle)
    validate_model_bundle(bundle)
    return bundle


def save_evaluation(evaluation: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evaluation, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
