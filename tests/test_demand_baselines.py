import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from scripts.backtest_baselines import read_demand_source, write_artifact
from src.data_processing.storage import PartitionedParquetStore
from src.models.demand_baselines import backtest_baselines


def demand_frame(days=9, *, start="2026-01-01T00:00:00Z"):
    timestamps = pd.date_range(start, periods=days * 96, freq="15min")
    step = np.arange(len(timestamps), dtype=float)
    return pd.DataFrame(
        {"timestamp": timestamps, "consumption_mw": 40_000 + step}
    )


def test_direct_baselines_have_exact_alignment_and_no_leakage():
    result = backtest_baselines(demand_frame())
    predictions = result.predictions

    assert set(predictions["horizon_hours"]) == {1, 3, 6, 24}
    assert set(predictions["baseline"]) == {"persistence", "day_naive", "week_naive"}
    assert (predictions["target"] - predictions["origin"]).eq(
        pd.to_timedelta(predictions["horizon_hours"], unit="h")
    ).all()
    assert predictions["source_timestamp"].le(predictions["origin"]).all()

    row = predictions.loc[
        predictions["baseline"].eq("day_naive")
        & predictions["horizon_hours"].eq(3)
        & predictions["origin"].eq(pd.Timestamp("2026-01-02T00:00:00Z"))
    ].iloc[0]
    assert row["target"] == pd.Timestamp("2026-01-02T03:00:00Z")
    assert row["source_timestamp"] == pd.Timestamp("2026-01-01T03:00:00Z")
    assert row["predicted_mw"] == 40_012
    assert row["actual_mw"] == 40_108


def test_metrics_are_correct_and_missing_targets_reduce_counts():
    frame = demand_frame()
    missing_timestamp = pd.Timestamp("2026-01-08T12:00:00Z")
    frame.loc[frame["timestamp"].eq(missing_timestamp), "consumption_mw"] = np.nan
    result = backtest_baselines(frame, horizons_hours=[1])
    persistence = result.metrics.loc[result.metrics["baseline"].eq("persistence")].iloc[0]

    # Linear demand increases four MW per hour, so persistence has exact 4 MW error.
    assert persistence["mae_mw"] == pytest.approx(4.0)
    assert persistence["rmse_mw"] == pytest.approx(4.0)
    assert persistence["smape_percent"] > 0
    assert persistence["missing_target_count"] == 1
    assert persistence["sample_count"] == persistence["available_target_count"] - 1
    assert 0 < persistence["coverage"] < 1


def test_rejects_duplicate_or_off_grid_timestamps():
    duplicate = pd.concat([demand_frame(1), demand_frame(1).iloc[[0]]], ignore_index=True)
    with pytest.raises(ValueError, match="unique"):
        backtest_baselines(duplicate)

    off_grid = demand_frame(1)
    off_grid.loc[0, "timestamp"] += pd.Timedelta(minutes=1)
    with pytest.raises(ValueError, match="15-minute"):
        backtest_baselines(off_grid)


def test_artifact_is_byte_deterministic(tmp_path):
    result = backtest_baselines(demand_frame(), horizons_hours=[24])
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    write_artifact(result, first, "official-cache.parquet")
    write_artifact(result, second, "official-cache.parquet")

    assert first.read_bytes() == second.read_bytes()
    payload = json.loads(first.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["metrics"][0]["sample_count"] > 0


def test_reads_agent2_partitioned_storage_contract(tmp_path):
    frame = demand_frame(days=2)
    frame["region"] = "France"
    root = tmp_path / "processed-store"
    PartitionedParquetStore(root).upsert(frame)

    loaded, source = read_demand_source(
        store_root=root,
        start="2026-01-01T12:00:00Z",
        end="2026-01-02T12:00:00Z",
        regions=["France"],
    )

    assert len(loaded) == 96
    assert source.startswith("partitioned-store:")
    assert set(loaded["region"]) == {"France"}


def test_cached_official_eco2mix_smoke_offline_safe():
    """Use real cached ODRE/RTE records when another task has fetched them."""
    processed = Path("data/processed/eco2mix_latest.parquet")
    caches = sorted(Path("data/raw/rte_eco2mix").glob("eco2mix_national_*.json"))
    if processed.exists():
        frame = pd.read_parquet(processed)
    elif caches:
        payload = json.loads(caches[-1].read_text(encoding="utf-8"))
        frame = pd.DataFrame(payload["results"])
    else:
        pytest.skip("No cached official RTE éCO2mix response is available offline")
    if len(frame) < 5:
        pytest.skip("Cached official response is shorter than one forecast horizon")
    result = backtest_baselines(frame, horizons_hours=[1])
    assert not result.predictions.empty
    assert result.predictions["source_timestamp"].le(result.predictions["origin"]).all()
