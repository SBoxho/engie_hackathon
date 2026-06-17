"""Run deterministic demand baseline backtests from persisted 15-minute data."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.models.demand_baselines import backtest_baselines, detect_demand_columns, infer_cadence, read_demand_file
from src.data_processing.storage import read_processed_data
from src.config import settings


def read_demand_source(
    *,
    input_path: Path | None = None,
    store_root: Path | None = None,
    start: str | None = None,
    end: str | None = None,
    regions: list[str] | None = None,
) -> tuple[pd.DataFrame, str]:
    """Read either one compatibility file or Agent 2's partitioned store contract."""
    if (input_path is None) == (store_root is None):
        raise ValueError("Provide exactly one of input_path or store_root.")
    if store_root is not None:
        frame = read_processed_data(store_root, start=start, end=end, regions=regions)
        return frame, f"partitioned-store:{store_root.resolve()}"
    assert input_path is not None
    return read_demand_file(input_path), str(input_path.resolve())


def _json_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    serializable = frame.copy()
    for column in serializable.select_dtypes(include=["datetimetz", "datetime"]).columns:
        serializable[column] = serializable[column].map(
            lambda value: value.isoformat().replace("+00:00", "Z") if pd.notna(value) else None
        )
    return json.loads(serializable.to_json(orient="records", double_precision=10))


def write_artifact(result: Any, output: Path, source: str, *, interval_minutes: int = 15) -> Path:
    payload = {
        "schema_version": 1,
        "method": "chronological rolling-origin direct seasonal-naive backtest",
        "interval_minutes": int(interval_minutes),
        "source": source,
        "metrics": _json_records(result.metrics),
        "predictions": _json_records(result.predictions),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return output


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--input", type=Path, help="Compatibility parquet, CSV, or JSON demand file")
    source.add_argument(
        "--store-root", type=Path, help="Root of Agent 2's year/month partitioned Parquet store"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=settings.baseline_artifact_path,
        help="Deterministic JSON artifact path",
    )
    parser.add_argument("--timestamp-column")
    parser.add_argument("--demand-column")
    parser.add_argument("--start", help="Inclusive ISO-8601 store read boundary")
    parser.add_argument("--end", help="Exclusive ISO-8601 store read boundary")
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=90,
        help="When --start is omitted, evaluate only this many days before the latest observation",
    )
    parser.add_argument("--region", action="append", dest="regions", help="Region filter; repeatable")
    parser.add_argument("--cadence-minutes", type=int, help="Override inferred demand cadence")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.lookback_days < 1:
        raise SystemExit("--lookback-days must be positive")
    frame, source = read_demand_source(
        input_path=args.input,
        store_root=args.store_root or (None if args.input else settings.energy_store_dir),
        start=args.start,
        end=args.end,
        regions=args.regions,
    )
    if args.start is None and not frame.empty:
        detected_timestamp, _ = detect_demand_columns(frame)
        timestamp_column = args.timestamp_column or detected_timestamp
        timestamps = pd.to_datetime(frame[timestamp_column], utc=True)
        cutoff = timestamps.max() - pd.Timedelta(days=args.lookback_days)
        frame = frame.loc[timestamps >= cutoff].copy()
    result = backtest_baselines(
        frame,
        timestamp_col=args.timestamp_column,
        demand_col=args.demand_column,
        cadence_minutes=args.cadence_minutes,
    )
    interval = infer_cadence(
        result.predictions.rename(columns={"origin": "timestamp"}),
        args.cadence_minutes,
    )
    artifact = write_artifact(
        result,
        args.output,
        source,
        interval_minutes=int(interval / pd.Timedelta(minutes=1)),
    )
    print(f"Wrote {len(result.predictions):,} predictions and {len(result.metrics)} metric rows to {artifact}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
