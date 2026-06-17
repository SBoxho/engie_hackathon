"""Build leakage-safe supervised features for demand model training."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.backtest_baselines import read_demand_source
from src.config import settings
from src.models.demand_model import FeatureConfig, build_feature_frame, save_feature_metadata


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--input", type=Path, help="Compatibility parquet, CSV, or JSON demand file")
    source.add_argument("--store-root", type=Path, help="Year/month partitioned processed eco2mix store")
    parser.add_argument("--weather", type=Path, default=settings.weather_features_path)
    parser.add_argument("--output", type=Path, default=settings.processed_dir / "demand_model" / "features.parquet")
    parser.add_argument("--metadata-output", type=Path, default=settings.processed_dir / "demand_model" / "feature_metadata.json")
    parser.add_argument("--start", help="Inclusive ISO-8601 store read boundary")
    parser.add_argument("--end", help="Exclusive ISO-8601 store read boundary")
    parser.add_argument("--region", action="append", dest="regions", help="Region filter; repeatable")
    parser.add_argument("--min-continuous-hours", type=float, default=48.0)
    parser.add_argument(
        "--cadence-minutes",
        type=int,
        help="Override inferred demand cadence; by default the observed mode is used",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    energy, source = read_demand_source(
        input_path=args.input,
        store_root=args.store_root or (None if args.input else settings.energy_store_dir),
        start=args.start,
        end=args.end,
        regions=args.regions or ["France"],
    )
    weather = pd.read_parquet(args.weather) if args.weather.exists() else None
    features, metadata = build_feature_frame(
        energy,
        weather=weather,
        config=FeatureConfig(
            min_continuous_hours=args.min_continuous_hours,
            cadence_minutes=args.cadence_minutes,
        ),
        source=source,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    features.to_parquet(args.output, index=False)
    save_feature_metadata(metadata, args.metadata_output)
    audit = metadata["audit"]
    weather_audit = audit.get("weather") or {}
    print(
        "Wrote "
        f"{len(features):,} feature rows to {args.output}; "
        f"coverage {audit.get('start_utc')} -> {audit.get('end_utc')}; "
        f"cadence {audit.get('cadence_minutes')}min; "
        f"missing intervals {audit.get('missing_interval_count')}; "
        f"weather overlap {weather_audit.get('overlap_fraction_of_energy_timestamps', 0):.1%}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
