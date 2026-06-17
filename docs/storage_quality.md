# Partitioned storage and data quality

## Stable public contract

These names and signatures are frozen for agents 1, 4, and 5:

```python
from src.data_processing.storage import (
    PartitionedParquetStore,
    UpdateResult,
    read_processed_data,
    upsert_processed_data,
)
from src.data_processing.quality import (
    QualityConfig,
    QualityFinding,
    QualityReport,
    run_quality_checks,
)

store = PartitionedParquetStore(root, timestamp_column="timestamp", region_column="region")
result: UpdateResult = store.upsert(frame)
frame = store.read(start=None, end=None, regions=None, columns=None)  # [start, end)
report: QualityReport = run_quality_checks(frame, now=None, config=None)
```

The functional wrappers are `upsert_processed_data(frame, root)` and
`read_processed_data(root, **filters)`. The merge key is `(timestamp, region)`,
timestamps are normalized to UTC, and the last incoming duplicate wins. `UpdateResult`
reports received, stored, inserted, replaced, and touched partitions.

## Storage behavior

Processed files use `year=YYYY/month=MM/data.parquet`. Reads inspect only months
intersecting the requested half-open range, then apply exact timestamp and region
filters. Every partition write creates and validates a temporary Parquet file before
atomic replacement. A validated last-known-good `.bak` permits recovery if the target
is absent or malformed; abandoned temporary files are removed. A per-partition lock
prevents concurrent lost updates. Atomicity is per partition, not across a multi-month
batch.

The implementation rejects a store under `data/raw`; fetching remains responsible for
immutable raw snapshots. Suspicious processed rows are never silently removed.

## Quality classifications

`run_quality_checks` reports schema, timestamp validity, 15-minute per-region cadence,
duplicate keys, estimated missing intervals, per-column null percentage, nonnegative
generation, bounded shares, stale latest timestamp, broad physical extremes, and the
supply-demand balance residual. Findings have a check, classification, severity,
count, and message. `QualityReport.suspicious_rows` retains source values and adds the
check, severity, and original index; one row may appear for multiple findings.

Defaults are intentionally transparent in `QualityConfig`: null warning above 5%,
stale after 90 minutes, power magnitude above 200 GW, CO2 intensity outside 0–2000
g/kWh, and balance residual above max(1 GW, 5% of demand). These are operational
screening thresholds, not source corrections.

## Official source and reproducibility

The application source is the ODRÉ/RTE real-time national éCO2mix dataset
`eco2mix-national-tr`, accessed through the official Opendatasoft Explore API v2.1:

- Dataset: <https://odre.opendatasoft.com/explore/dataset/eco2mix-national-tr/>
- API records endpoint: <https://odre.opendatasoft.com/api/explore/v2.1/catalog/datasets/eco2mix-national-tr/records>
- API documentation: <https://help.opendatasoft.com/apis/ods-explore-v2/>

The existing source adapter requests populated observations and preserves fetched raw
JSON. The opt-in smoke test performs a real API request but is skipped by default so
the suite is offline-safe:

```powershell
$env:RUN_LIVE_DATA_SMOKE='1'
pytest -q tests/test_quality.py -k live_smoke
```

## Commands and lead integration

Incremental online update:

```powershell
python scripts/update_data.py --hours 72
```

Use `--offline` to process the latest immutable raw cache. Run checks with:

```powershell
python scripts/run_quality_checks.py --start 2026-01-01 --region France --evidence data/interim/suspicious.parquet
```

Lead integration (shared files intentionally untouched): replace the single-file write
in `scripts/fetch_data.py` with `PartitionedParquetStore(settings.processed_dir /
"eco2mix").upsert(clean)`, or invoke `scripts/update_data.py`. In `app/main.py`, load a
pruned range via `store.read(...)` and call
`app.components.data_quality.render_data_quality(frame)`. No changes to configuration,
requirements, README, or the shared fetch/app entry points are required.
