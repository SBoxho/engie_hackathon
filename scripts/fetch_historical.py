"""Fetch a bounded consolidated national éCO2mix interval."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import settings
from src.data_processing.features import add_time_features
from src.data_processing.storage import PartitionedParquetStore
from src.data_sources.rte_eco2mix_historical import fetch_historical, fetch_historical_raw
from src.utils.io import write_dataframe


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument("--start", required=True, help="inclusive ISO date/datetime")
    command.add_argument("--end", required=True, help="exclusive ISO date/datetime")
    command.add_argument("--output", type=Path, help="clean .csv or .parquet destination")
    command.add_argument("--raw-only", action="store_true", help="fetch/cache without standardizing")
    command.add_argument("--no-cache", action="store_true", help="do not write immutable raw snapshot")
    command.add_argument("--store", type=Path, default=settings.energy_store_dir)
    return command


def main() -> int:
    args = parser().parse_args()
    kwargs = {"cache": not args.no_cache}
    frame = (
        fetch_historical_raw(args.start, args.end, **kwargs)
        if args.raw_only
        else fetch_historical(args.start, args.end, **kwargs)
    )
    update_result = None
    if not args.raw_only:
        frame = add_time_features(frame, settings.timezone)
        update_result = PartitionedParquetStore(args.store).upsert(frame)
    if args.output:
        output = args.output
    else:
        suffix = "raw" if args.raw_only else "clean"
        output = settings.processed_dir / f"eco2mix_historical_{args.start}_{args.end}_{suffix}.parquet"
    write_dataframe(frame, output)
    message = f"Wrote {len(frame):,} rows to {output}"
    if update_result is not None:
        message += (
            f"; partitioned store inserted={update_result.inserted_rows} "
            f"replaced={update_result.replaced_rows}"
        )
    print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
