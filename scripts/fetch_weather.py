"""Fetch and aggregate population-weighted national weather features."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import settings
from src.data_processing.storage import read_processed_data
from src.data_processing.weather_features import build_national_weather_features, join_energy_weather
from src.data_sources.weather_national import fetch_national_weather, load_city_reference
from src.utils.io import write_dataframe


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="First date (YYYY-MM-DD)")
    parser.add_argument("--end", required=True, help="Last date (YYYY-MM-DD)")
    parser.add_argument("--cities", nargs="*", help="Optional city ids; defaults to all reference cities")
    parser.add_argument("--cache-dir", type=Path, default=Path("data/raw/weather_national"))
    parser.add_argument("--output", type=Path, default=settings.weather_features_path)
    parser.add_argument("--energy-store", type=Path, default=settings.energy_store_dir)
    parser.add_argument("--joined-output", type=Path, default=settings.joined_features_path)
    parser.add_argument("--force-refresh", action="store_true")
    parser.add_argument("--strict", action="store_true", help="Fail on any unavailable city")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cities, metadata = load_city_reference()
    if args.cities:
        requested = set(args.cities)
        cities = [city for city in cities if city.id in requested]
        unknown = requested - {city.id for city in cities}
        if unknown:
            raise SystemExit(f"Unknown city ids: {', '.join(sorted(unknown))}")
    raw = fetch_national_weather(
        args.start, args.end, cities=cities, cache_dir=args.cache_dir,
        force_refresh=args.force_refresh, strict=args.strict,
    )
    targets = pd.date_range(
        pd.Timestamp(args.start, tz="UTC"),
        pd.Timestamp(args.end, tz="UTC") + pd.Timedelta(days=1) - pd.Timedelta(minutes=15),
        freq="15min",
    )
    features = build_national_weather_features(raw, targets, cities=cities)
    write_dataframe(features, args.output)
    energy = read_processed_data(
        args.energy_store,
        start=pd.Timestamp(args.start, tz="UTC"),
        end=pd.Timestamp(args.end, tz="UTC") + pd.Timedelta(days=1),
    )
    joined_rows = 0
    if not energy.empty:
        joined = join_energy_weather(energy, features)
        write_dataframe(joined, args.joined_output)
        joined_rows = len(joined)
    failures = raw.attrs.get("fetch_failures", {})
    print(
        f"Wrote {len(features)} UTC quarter-hours to {args.output} from "
        f"{features['weather_city_count'].max()} cities; reference={metadata['reference_id']}; "
        f"failures={failures}; joined_energy_rows={joined_rows}"
    )


if __name__ == "__main__":
    main()
