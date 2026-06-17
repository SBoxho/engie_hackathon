"""Run quality checks over a pruned range of the processed store."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import settings
from src.data_processing.quality import run_quality_checks
from src.data_processing.storage import PartitionedParquetStore


def main() -> int:
    parser = argparse.ArgumentParser(description="Check processed eco2mix data quality")
    parser.add_argument("--store", type=Path, default=settings.processed_dir / "eco2mix")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--region", action="append", dest="regions")
    parser.add_argument("--evidence", type=Path, help="Optional suspicious-row Parquet output")
    args = parser.parse_args()

    frame = PartitionedParquetStore(args.store).read(
        start=args.start, end=args.end, regions=args.regions
    )
    report = run_quality_checks(frame)
    print(json.dumps(report.to_dict(), indent=2))
    if args.evidence and not report.suspicious_rows.empty:
        args.evidence.parent.mkdir(parents=True, exist_ok=True)
        report.suspicious_rows.to_parquet(args.evidence, index=False)
    return 0 if report.passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
