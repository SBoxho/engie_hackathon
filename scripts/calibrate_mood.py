"""Create a mood calibration artifact from Agent 2 partitions or a data file."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.data_processing.storage import read_processed_data
from src.models.mood_calibration import calibrate_mood
from src.config import settings


def read_calibration_source(
    path: Path | None,
    store_root: Path | None,
    *,
    start: str | None = None,
    end: str | None = None,
) -> tuple[pd.DataFrame, str]:
    """Read one compatibility file or Agent 2's partition-pruned store."""
    if path is not None:
        if path.suffix.lower() in {".parquet", ".pq"}:
            frame = pd.read_parquet(path)
        elif path.suffix.lower() == ".csv":
            frame = pd.read_csv(path)
        else:
            raise ValueError("--input must be a .parquet, .pq, or .csv file")
        return frame, str(path.resolve())
    if store_root is None:
        raise ValueError("provide --store-root (preferred) or --input")
    frame = read_processed_data(
        store_root,
        start=start,
        end=end,
        regions=["France"],
        columns=["timestamp", *REQUIRED_COLUMNS],
    )
    return frame, f"partitioned-store:{store_root.resolve()}"


REQUIRED_COLUMNS = (
    "consumption_mw",
    "co2_intensity_g_per_kwh",
    "renewable_share",
    "fossil_share",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, help="Interoperability fallback; prefer partition reader")
    parser.add_argument("--store-root", type=Path, help="Agent 2 partitioned processed-data root")
    parser.add_argument("--start", help="Inclusive UTC/ISO lower bound for partition reads")
    parser.add_argument("--end", help="Exclusive UTC/ISO upper bound for partition reads")
    parser.add_argument("--output", type=Path, default=settings.mood_artifact_path)
    parser.add_argument("--min-sample", type=int, default=30)
    parser.add_argument("--generated-at", help="Explicit ISO timestamp for reproducible artifacts")
    args = parser.parse_args()

    frame, source = read_calibration_source(
        args.input,
        args.store_root or (None if args.input else settings.energy_store_dir),
        start=args.start,
        end=args.end,
    )
    artifact = calibrate_mood(
        frame,
        min_sample=args.min_sample,
        generated_at=args.generated_at,
        source_name=f"RTE eCO2mix via ODRÉ ({source})",
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {len(artifact['segments'])} calibrated segments to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
