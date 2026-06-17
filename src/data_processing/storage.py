"""Incremental, partition-pruned Parquet storage for processed grid data.

Public contract
---------------
``PartitionedParquetStore.upsert(frame)`` atomically merges by
``(timestamp, region)`` and returns :class:`UpdateResult`.
``PartitionedParquetStore.read(...)`` reads only intersecting year/month
partitions. Raw source files are deliberately outside this module's scope.
"""
from __future__ import annotations

import os
import shutil
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

import pandas as pd


@dataclass(frozen=True)
class UpdateResult:
    received_rows: int
    stored_rows: int
    inserted_rows: int
    replaced_rows: int
    partitions_written: tuple[str, ...]


class StorageError(RuntimeError):
    """Raised when the processed store cannot be read or recovered safely."""


class _PartitionLock:
    def __init__(self, path: Path, timeout: float = 10.0) -> None:
        self.path = path
        self.timeout = timeout
        self.fd: int | None = None

    def __enter__(self) -> "_PartitionLock":
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, f"pid={os.getpid()}\n".encode())
                return self
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise StorageError(f"Timed out waiting for partition lock {self.path}")
                time.sleep(0.05)

    def __exit__(self, *_: object) -> None:
        if self.fd is not None:
            os.close(self.fd)
        self.path.unlink(missing_ok=True)


class PartitionedParquetStore:
    """Year/month partitioned store with per-partition atomic replacement.

    Incoming duplicate keys use last-write-wins semantics, including duplicates
    within one input frame. Timestamps are stored in UTC.
    """

    filename = "data.parquet"

    def __init__(
        self,
        root: str | Path,
        *,
        timestamp_column: str = "timestamp",
        region_column: str = "region",
    ) -> None:
        self.root = Path(root)
        self.timestamp_column = timestamp_column
        self.region_column = region_column
        lowered_parts = [part.lower() for part in self.root.parts]
        if "raw" in lowered_parts and "data" in lowered_parts:
            raise ValueError("Processed partition storage must not be placed under data/raw")

    @property
    def key_columns(self) -> tuple[str, str]:
        return self.timestamp_column, self.region_column

    def _normalise(self, frame: pd.DataFrame) -> pd.DataFrame:
        missing = set(self.key_columns).difference(frame.columns)
        if missing:
            raise ValueError(f"Frame is missing storage key columns: {sorted(missing)}")
        result = frame.copy()
        result[self.timestamp_column] = pd.to_datetime(
            result[self.timestamp_column], utc=True, errors="coerce"
        )
        if result[self.timestamp_column].isna().any():
            raise ValueError("Storage key contains invalid or missing timestamps")
        if result[self.region_column].isna().any():
            raise ValueError("Storage key contains missing regions")
        result[self.region_column] = result[self.region_column].astype(str)
        return result

    def _partition_dir(self, year: int, month: int) -> Path:
        return self.root / f"year={year:04d}" / f"month={month:02d}"

    @staticmethod
    def _validate_parquet(path: Path) -> pd.DataFrame:
        try:
            return pd.read_parquet(path)
        except Exception as exc:
            raise StorageError(f"Unreadable Parquet partition {path}: {exc}") from exc

    def _recover(self, directory: Path) -> None:
        """Recover a corrupt/missing target from its last known-good backup."""
        target = directory / self.filename
        backup = directory / f"{self.filename}.bak"
        for temporary in directory.glob(f".{self.filename}.*.tmp"):
            temporary.unlink(missing_ok=True)
        if target.exists():
            try:
                self._validate_parquet(target)
                return
            except StorageError:
                pass
        if backup.exists():
            self._validate_parquet(backup)
            os.replace(backup, target)
            return
        if target.exists():
            raise StorageError(f"Partition is corrupt and has no valid backup: {target}")

    def _atomic_write(self, frame: pd.DataFrame, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / self.filename
        backup = directory / f"{self.filename}.bak"
        temporary = directory / f".{self.filename}.{uuid.uuid4().hex}.tmp"
        backup_tmp = directory / f".{self.filename}.backup.{uuid.uuid4().hex}.tmp"
        try:
            frame.to_parquet(temporary, index=False)
            self._validate_parquet(temporary)
            if target.exists():
                shutil.copy2(target, backup_tmp)
                self._validate_parquet(backup_tmp)
                os.replace(backup_tmp, backup)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
            backup_tmp.unlink(missing_ok=True)

    def upsert(self, frame: pd.DataFrame) -> UpdateResult:
        incoming = self._normalise(frame)
        if incoming.empty:
            return UpdateResult(0, 0, 0, 0, ())
        received = len(incoming)
        incoming = incoming.drop_duplicates(list(self.key_columns), keep="last")
        incoming["_year"] = incoming[self.timestamp_column].dt.year
        incoming["_month"] = incoming[self.timestamp_column].dt.month
        inserted = replaced = stored = 0
        written: list[str] = []

        for (year, month), batch in incoming.groupby(["_year", "_month"], sort=True):
            directory = self._partition_dir(int(year), int(month))
            directory.mkdir(parents=True, exist_ok=True)
            with _PartitionLock(directory / ".write.lock"):
                self._recover(directory)
                target = directory / self.filename
                existing = self._normalise(pd.read_parquet(target)) if target.exists() else None
                clean_batch = batch.drop(columns=["_year", "_month"])
                if existing is None:
                    overlap = 0
                    merged = clean_batch
                else:
                    old_keys = pd.MultiIndex.from_frame(existing[list(self.key_columns)])
                    new_keys = pd.MultiIndex.from_frame(clean_batch[list(self.key_columns)])
                    overlap = int(new_keys.isin(old_keys).sum())
                    merged = pd.concat([existing, clean_batch], ignore_index=True, sort=False)
                    merged = merged.drop_duplicates(list(self.key_columns), keep="last")
                merged = merged.sort_values(list(self.key_columns)).reset_index(drop=True)
                self._atomic_write(merged, directory)
                replaced += overlap
                inserted += len(clean_batch) - overlap
                stored += len(merged)
                written.append(str(directory.relative_to(self.root)))
        return UpdateResult(received, stored, inserted, replaced, tuple(written))

    def _candidate_files(
        self, start: pd.Timestamp | None, end: pd.Timestamp | None
    ) -> Iterable[Path]:
        for directory in sorted(self.root.glob("year=*/month=*")):
            if not directory.is_dir():
                continue
            path = directory / self.filename
            if not path.exists() and not (directory / f"{self.filename}.bak").exists():
                continue
            try:
                year = int(directory.parent.name.split("=", 1)[1])
                month = int(directory.name.split("=", 1)[1])
                partition_start = pd.Timestamp(year=year, month=month, day=1, tz="UTC")
                partition_end = partition_start + pd.offsets.MonthBegin(1)
            except (ValueError, IndexError):
                continue
            if start is not None and partition_end <= start:
                continue
            if end is not None and partition_start >= end:
                continue
            yield path

    def read(
        self,
        *,
        start: str | datetime | pd.Timestamp | None = None,
        end: str | datetime | pd.Timestamp | None = None,
        regions: Iterable[str] | None = None,
        columns: Iterable[str] | None = None,
    ) -> pd.DataFrame:
        """Read ``[start, end)`` while pruning non-intersecting partitions."""
        start_ts = pd.to_datetime(start, utc=True) if start is not None else None
        end_ts = pd.to_datetime(end, utc=True) if end is not None else None
        if start_ts is not None and end_ts is not None and start_ts >= end_ts:
            raise ValueError("start must be earlier than end")
        region_values = set(regions) if regions is not None else None
        requested = list(columns) if columns is not None else None
        scan_columns = None
        if requested is not None:
            scan_columns = list(dict.fromkeys([*requested, *self.key_columns]))
        frames: list[pd.DataFrame] = []
        for path in self._candidate_files(start_ts, end_ts):
            self._recover(path.parent)
            part = pd.read_parquet(path, columns=scan_columns)
            part[self.timestamp_column] = pd.to_datetime(part[self.timestamp_column], utc=True)
            if start_ts is not None:
                part = part[part[self.timestamp_column] >= start_ts]
            if end_ts is not None:
                part = part[part[self.timestamp_column] < end_ts]
            if region_values is not None:
                part = part[part[self.region_column].isin(region_values)]
            if requested is not None:
                part = part[requested]
            frames.append(part)
        if not frames:
            return pd.DataFrame(columns=requested or [])
        return pd.concat(frames, ignore_index=True, sort=False)


def upsert_processed_data(frame: pd.DataFrame, root: str | Path) -> UpdateResult:
    """Functional convenience contract for agents that do not need a store object."""
    return PartitionedParquetStore(root).upsert(frame)


def read_processed_data(root: str | Path, **filters: object) -> pd.DataFrame:
    """Functional convenience contract; accepts the same filters as ``read``."""
    return PartitionedParquetStore(root).read(**filters)  # type: ignore[arg-type]
