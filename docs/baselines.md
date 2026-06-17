# Persistence and seasonal-naive demand baselines

This task adds transparent **non-AI** reference forecasts for national electricity demand. They are intended as a minimum bar for future forecasting models, not as production forecasts.

## Rules and horizons

The evaluator makes direct forecasts at 1, 3, 6, and 24 hours on an exact 15-minute UTC grid:

- **Persistence:** use demand observed at the forecast origin.
- **Day-naive:** use demand at `target - 24 hours`.
- **Week-naive:** use demand at `target - 7 days`.

For every row, `source_timestamp <= origin`; this is checked at runtime and in tests. Targets and source observations are exact timestamp joins. No interpolation, backward fill, or resampling is performed, so unavailable values remain visible.

The rolling-origin evaluation walks chronologically over every possible 15-minute origin. Each horizon is evaluated independently. Error metrics use only rows where both actual and prediction exist:

- MAE and RMSE in MW
- symmetric MAPE in percent, using `200 * |actual - prediction| / (|actual| + |prediction|)` and defining two zero values as 0%
- sample count, available-target count, missing-target count, missing-prediction count
- coverage = usable pairs / available targets

## Run

Use Agent 2's partitioned processed store directly:

```powershell
python scripts/backtest_baselines.py --store-root data/processed/grid --region France
```

The CLI calls the public `src.data_processing.storage.read_processed_data` contract and supports its partition-pruning `--start`, `--end`, and repeatable `--region` filters. For migration/debugging, a narrow compatibility reader also accepts one parquet, CSV, or JSON file and recognizes `timestamp`/`consumption_mw` and source-native `date_heure`/`consommation` columns:

```powershell
python scripts/backtest_baselines.py --input data/processed/<demand-file>.parquet
```

The deterministic artifact is written to `data/processed/baseline_backtest.json`. It deliberately omits a generation timestamp and has stable row/key ordering, so identical source data produces byte-identical output. Override column names or output location with `--timestamp-column`, `--demand-column`, and `--output`.

Open the Streamlit **Demand baselines** page to select a rule and horizon, compare actual demand with the baseline, inspect the evaluation period and metrics, and see missing-data coverage.

## Official data and offline behavior

Demand comes from RTE's national éCO2mix real-time dataset published through the official ODRE Opendatasoft API (`eco2mix-national-tr`). The project fetcher excludes forecast rows with null observed consumption. Unit tests are offline-safe: the real-data smoke test uses the latest existing raw official cache and skips when no cache is present; it never calls the network.

Source: [ODRE éCO2mix national real-time dataset](https://odre.opendatasoft.com/explore/dataset/eco2mix-national-tr/)

## Storage integration contract

The baseline module does not create or duplicate a storage system. The CLI consumes `src/data_processing/storage.py` and keeps its fallback file adapter intentionally narrow. `backtest_baselines()` accepts an in-memory DataFrame, and the dashboard consumes the deterministic result artifact rather than bypassing storage to read raw data.
