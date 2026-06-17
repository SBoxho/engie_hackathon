from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

from src.config import settings
from src.data_processing.clean_energy_mix import clean_energy_mix
from src.data_processing.features import add_time_features
from src.data_processing.storage import PartitionedParquetStore
from src.data_sources.rte_eco2mix import fetch_eco2mix
from src.utils.io import write_dataframe


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch and process official RTE éCO2mix data")
    parser.add_argument("--hours", type=int, default=settings.history_hours)
    args = parser.parse_args()
    if args.hours < 1:
        parser.error("--hours must be positive")
    end = datetime.now(timezone.utc)
    raw = fetch_eco2mix(start=end - timedelta(hours=args.hours), end=end)
    clean = add_time_features(clean_energy_mix(raw), settings.timezone)
    output = settings.processed_dir / "eco2mix_latest.parquet"
    write_dataframe(clean, output)
    result = PartitionedParquetStore(settings.energy_store_dir).upsert(clean)
    print(
        f"Saved {len(clean)} processed rows to {output}; "
        f"partitioned store inserted={result.inserted_rows} replaced={result.replaced_rows}"
    )


if __name__ == "__main__":
    main()
