"""Train deterministic direct HistGradientBoosting demand models."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import settings
from src.models.demand_model import TrainConfig, load_feature_metadata, save_model_bundle, train_models


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--features", type=Path, default=settings.processed_dir / "demand_model" / "features.parquet")
    parser.add_argument("--metadata", type=Path, default=settings.processed_dir / "demand_model" / "feature_metadata.json")
    parser.add_argument("--output", type=Path, default=settings.processed_dir / "demand_model" / "demand_hgb_model.pkl")
    parser.add_argument("--test-fraction", type=float, default=0.2)
    parser.add_argument("--validation-folds", type=int, default=3)
    parser.add_argument("--min-train-samples", type=int, default=96)
    parser.add_argument("--min-test-samples", type=int, default=24)
    parser.add_argument("--seed", type=int, default=42)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    features = pd.read_parquet(args.features)
    metadata = load_feature_metadata(args.metadata)
    bundle = train_models(
        features,
        metadata,
        config=TrainConfig(
            random_seed=args.seed,
            test_fraction=args.test_fraction,
            validation_folds=args.validation_folds,
            min_train_samples=args.min_train_samples,
            min_test_samples=args.min_test_samples,
        ),
    )
    save_model_bundle(bundle, args.output)
    print(
        f"Wrote {bundle['model_kind']} artifact with horizons {bundle['horizons']} to {args.output}"
    )
    if bundle.get("skipped_horizons"):
        print(f"Skipped horizons: {bundle['skipped_horizons']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
