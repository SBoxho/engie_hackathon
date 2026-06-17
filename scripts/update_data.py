"""Incrementally update processed Parquet data from official or cached raw data."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import settings
from src.data_processing.clean_energy_mix import clean_energy_mix
from src.data_processing.features import add_time_features
from src.data_processing.quality import run_quality_checks
from src.data_processing.storage import PartitionedParquetStore
from src.data_sources.rte_eco2mix import fetch_eco2mix, load_cached_eco2mix
from src.data_sources.rte_eco2mix_historical import fetch_historical


def main() -> int:
    parser = argparse.ArgumentParser(description="Incrementally update processed eco2mix partitions")
    parser.add_argument("--hours", type=int, default=settings.history_hours)
    parser.add_argument("--start", help="Historical range start (inclusive)")
    parser.add_argument("--end", help="Historical range end (exclusive)")
    parser.add_argument("--store", type=Path, default=settings.energy_store_dir)
    parser.add_argument("--offline", action="store_true", help="Use the latest immutable raw cache")
    args = parser.parse_args()
    if args.hours < 1:
        parser.error("--hours must be positive")

    if bool(args.start) != bool(args.end):
        parser.error("--start and --end must be supplied together")
    if args.offline and args.start:
        parser.error("--offline cannot be combined with a historical range")

    if args.start:
        clean = add_time_features(fetch_historical(args.start, args.end), settings.timezone)
    elif args.offline:
        raw = load_cached_eco2mix()
        clean = add_time_features(clean_energy_mix(raw), settings.timezone)
    else:
        end = datetime.now(timezone.utc)
        raw = fetch_eco2mix(start=end - timedelta(hours=args.hours), end=end)
        clean = add_time_features(clean_energy_mix(raw), settings.timezone)
    quality = run_quality_checks(clean)
    result = PartitionedParquetStore(args.store).upsert(clean)
    print(
        f"received={result.received_rows} inserted={result.inserted_rows} "
        f"replaced={result.replaced_rows} partitions={len(result.partitions_written)} "
        f"quality_passed={quality.passed} suspicious={len(quality.suspicious_rows)}"
    )
    return 0 if quality.passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
