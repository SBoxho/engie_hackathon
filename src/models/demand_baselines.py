"""Leakage-safe demand baselines and chronological backtesting.

Every forecast is made at an ``origin`` using only an observation whose
timestamp is less than or equal to that origin. Targets are joined by exact
timestamp; gaps are never filled or interpolated.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pandas as pd


INTERVAL = pd.Timedelta(minutes=15)
HORIZON_HOURS = (1, 3, 6, 24)
BASELINE_LAGS = {
    "persistence": None,
    "day_naive": pd.Timedelta(days=1),
    "week_naive": pd.Timedelta(days=7),
}
TIMESTAMP_COLUMNS = ("timestamp", "date_heure", "datetime", "time")
DEMAND_COLUMNS = ("consumption_mw", "consommation", "demand_mw", "demand")


@dataclass(frozen=True)
class BacktestResult:
    """Prediction-level output and its grouped metrics."""

    predictions: pd.DataFrame
    metrics: pd.DataFrame


def detect_demand_columns(frame: pd.DataFrame) -> tuple[str, str]:
    """Return the first supported timestamp and demand column names."""
    timestamp = next((column for column in TIMESTAMP_COLUMNS if column in frame), None)
    demand = next((column for column in DEMAND_COLUMNS if column in frame), None)
    if timestamp is None or demand is None:
        raise ValueError(
            "Demand data needs one timestamp column "
            f"{TIMESTAMP_COLUMNS} and one demand column {DEMAND_COLUMNS}."
        )
    return timestamp, demand


def infer_cadence(frame: pd.DataFrame, cadence_minutes: int | None = None) -> pd.Timedelta:
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


def normalize_demand(
    frame: pd.DataFrame,
    *,
    timestamp_col: str | None = None,
    demand_col: str | None = None,
) -> pd.DataFrame:
    """Normalize demand to sorted UTC timestamps while preserving missing values."""
    if timestamp_col is None or demand_col is None:
        detected_timestamp, detected_demand = detect_demand_columns(frame)
        timestamp_col = timestamp_col or detected_timestamp
        demand_col = demand_col or detected_demand
    normalized = frame[[timestamp_col, demand_col]].rename(
        columns={timestamp_col: "timestamp", demand_col: "actual_mw"}
    ).copy()
    normalized["timestamp"] = pd.to_datetime(normalized["timestamp"], utc=True, errors="coerce")
    if normalized["timestamp"].isna().any():
        raise ValueError("Demand data contains invalid or missing timestamps.")
    normalized["actual_mw"] = pd.to_numeric(normalized["actual_mw"], errors="coerce")
    normalized = normalized.sort_values("timestamp", kind="stable").reset_index(drop=True)
    if normalized["timestamp"].duplicated().any():
        raise ValueError("Demand timestamps must be unique for exact target alignment.")
    off_grid = (
        normalized["timestamp"].dt.minute.mod(15).ne(0)
        | normalized["timestamp"].dt.second.ne(0)
        | normalized["timestamp"].dt.microsecond.ne(0)
    )
    if off_grid.any():
        raise ValueError("Demand timestamps must lie on an exact 15-minute grid.")
    return normalized


def backtest_baselines(
    frame: pd.DataFrame,
    *,
    horizons_hours: Iterable[int] = HORIZON_HOURS,
    timestamp_col: str | None = None,
    demand_col: str | None = None,
    cadence_minutes: int | None = None,
) -> BacktestResult:
    """Run direct rolling-origin baselines over every eligible 15-minute origin.

    An eligible origin is every grid timestamp from the first observation until
    the final target can still fall inside the observed period. Missing origins,
    lagged observations, and targets remain explicit as null predictions/actuals.
    """
    demand = normalize_demand(frame, timestamp_col=timestamp_col, demand_col=demand_col)
    if demand.empty:
        raise ValueError("Demand data is empty.")
    cadence = infer_cadence(demand, cadence_minutes)
    horizons = tuple(sorted(set(int(value) for value in horizons_hours)))
    if not horizons or any(value <= 0 or value > 24 for value in horizons):
        raise ValueError("Horizons must be unique positive whole hours up to 24.")

    values = demand.set_index("timestamp")["actual_mw"]
    final_timestamp = demand["timestamp"].iloc[-1]
    prediction_parts: list[pd.DataFrame] = []
    for horizon in horizons:
        delta = pd.Timedelta(hours=horizon)
        last_origin = final_timestamp - delta
        if last_origin < demand["timestamp"].iloc[0]:
            continue
        origins = pd.date_range(
            demand["timestamp"].iloc[0], last_origin, freq=cadence, tz="UTC"
        )
        targets = origins + delta
        actual = values.reindex(targets).to_numpy()
        for baseline, seasonal_lag in BASELINE_LAGS.items():
            source_times = origins if seasonal_lag is None else targets - seasonal_lag
            if (source_times > origins).any():
                raise ValueError(
                    f"{baseline} at {horizon}h would use observations after the forecast origin."
                )
            prediction_parts.append(
                pd.DataFrame(
                    {
                        "origin": origins,
                        "target": targets,
                        "source_timestamp": source_times,
                        "horizon_hours": horizon,
                        "baseline": baseline,
                        "actual_mw": actual,
                        "predicted_mw": values.reindex(source_times).to_numpy(),
                    }
                )
            )
    if not prediction_parts:
        raise ValueError("Demand history is shorter than the smallest requested horizon.")
    predictions = pd.concat(prediction_parts, ignore_index=True).sort_values(
        ["target", "horizon_hours", "baseline"], kind="stable", ignore_index=True
    )
    metrics = _metrics(predictions)
    return BacktestResult(predictions=predictions, metrics=metrics)


def _metrics(predictions: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, float | int | str]] = []
    for (baseline, horizon), group in predictions.groupby(
        ["baseline", "horizon_hours"], sort=True
    ):
        target_available = group["actual_mw"].notna()
        prediction_available = group["predicted_mw"].notna()
        valid = target_available & prediction_available
        actual = group.loc[valid, "actual_mw"].astype(float)
        predicted = group.loc[valid, "predicted_mw"].astype(float)
        absolute_error = (actual - predicted).abs()
        squared_error = (actual - predicted).pow(2)
        denominator = actual.abs() + predicted.abs()
        smape_terms = (200.0 * absolute_error / denominator).where(denominator.ne(0), 0.0)
        sample_count = int(valid.sum())
        available_targets = int(target_available.sum())
        rows.append(
            {
                "baseline": str(baseline),
                "horizon_hours": int(horizon),
                "mae_mw": float(absolute_error.mean()) if sample_count else float("nan"),
                "rmse_mw": float(squared_error.mean() ** 0.5) if sample_count else float("nan"),
                "smape_percent": float(smape_terms.mean()) if sample_count else float("nan"),
                "sample_count": sample_count,
                "origin_count": int(len(group)),
                "available_target_count": available_targets,
                "missing_target_count": int((~target_available).sum()),
                "missing_prediction_count": int((target_available & ~prediction_available).sum()),
                "coverage": sample_count / available_targets if available_targets else 0.0,
            }
        )
    return pd.DataFrame(rows).sort_values(
        ["horizon_hours", "baseline"], kind="stable", ignore_index=True
    )


def read_demand_file(path: str | Path) -> pd.DataFrame:
    """Narrow file adapter used by the CLI and dashboard, not a storage layer."""
    path = Path(path)
    if path.suffix.lower() in {".parquet", ".pq"}:
        return pd.read_parquet(path)
    if path.suffix.lower() == ".csv":
        return pd.read_csv(path)
    if path.suffix.lower() == ".json":
        return pd.read_json(path)
    raise ValueError(f"Unsupported demand file: {path}. Use parquet, CSV, or JSON.")
