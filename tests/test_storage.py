from __future__ import annotations

import os

import pandas as pd
import pytest

from src.data_processing.storage import PartitionedParquetStore


def rows(*timestamps: str) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime(list(timestamps), utc=True),
            "region": ["France"] * len(timestamps),
            "consumption_mw": list(range(50_000, 50_000 + len(timestamps))),
        }
    )


def test_upsert_is_partitioned_idempotent_and_last_write_wins(tmp_path):
    store = PartitionedParquetStore(tmp_path / "processed")
    first = rows("2026-01-31T23:45Z", "2026-02-01T00:00Z")
    result = store.upsert(first)
    assert result.inserted_rows == 2
    assert set(result.partitions_written) == {"year=2026\\month=01", "year=2026\\month=02"}

    replacement = first.iloc[[1]].copy()
    replacement["consumption_mw"] = 99_999
    result = store.upsert(pd.concat([replacement, replacement], ignore_index=True))
    assert result.replaced_rows == 1
    assert result.inserted_rows == 0
    loaded = store.read()
    assert len(loaded) == 2
    assert loaded.loc[loaded["timestamp"] == replacement.iloc[0]["timestamp"], "consumption_mw"].item() == 99_999


def test_range_region_and_column_filters_are_applied(tmp_path):
    store = PartitionedParquetStore(tmp_path / "processed")
    frame = rows("2026-01-31T23:45Z", "2026-02-01T00:00Z", "2026-02-01T00:15Z")
    frame.loc[2, "region"] = "East"
    store.upsert(frame)
    loaded = store.read(
        start="2026-02-01", end="2026-03-01", regions=["France"], columns=["timestamp", "consumption_mw"]
    )
    assert list(loaded.columns) == ["timestamp", "consumption_mw"]
    assert loaded["timestamp"].tolist() == [pd.Timestamp("2026-02-01T00:00Z")]


def test_interrupted_write_recovers_backup_and_cleans_temporary(tmp_path):
    store = PartitionedParquetStore(tmp_path / "processed")
    original = rows("2026-01-01T00:00Z")
    store.upsert(original)
    directory = tmp_path / "processed" / "year=2026" / "month=01"
    target = directory / "data.parquet"
    backup = directory / "data.parquet.bak"
    os.replace(target, backup)
    interrupted = directory / ".data.parquet.abandoned.tmp"
    interrupted.write_bytes(b"partial parquet")

    recovered = store.read()
    assert recovered["consumption_mw"].tolist() == [50_000]
    assert target.exists()
    assert not interrupted.exists()


def test_malformed_target_recovers_last_known_good_backup(tmp_path):
    store = PartitionedParquetStore(tmp_path / "processed")
    original = rows("2026-01-01T00:00Z")
    store.upsert(original)
    changed = original.copy()
    changed["consumption_mw"] = 60_000
    store.upsert(changed)
    target = tmp_path / "processed" / "year=2026" / "month=01" / "data.parquet"
    target.write_bytes(b"not parquet")

    recovered = store.read()
    assert recovered["consumption_mw"].tolist() == [50_000]


def test_invalid_keys_and_raw_destination_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="data/raw"):
        PartitionedParquetStore(tmp_path / "data" / "raw" / "processed")
    store = PartitionedParquetStore(tmp_path / "processed")
    with pytest.raises(ValueError, match="invalid"):
        store.upsert(pd.DataFrame({"timestamp": ["bad"], "region": ["France"]}))
