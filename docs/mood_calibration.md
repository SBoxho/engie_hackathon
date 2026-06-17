# Mood calibration by season and local hour

## Purpose and limits

The mood is a transparent educational summary of observed French electricity data. It is **not** an RTE alert, a grid-security diagnosis, a forecast, or advice to alter consumption.

The implementation uses national RTE éCO2mix observations distributed through the official ODRÉ open-data API (`eco2mix-national-tr`). The processed inputs are consumption (MW), CO₂ intensity (g/kWh), and renewable/fossil generation shares. Calibration reads Agent 2's partitioned store through `read_processed_data`; it does not create another data store.

## Segmentation and DST

UTC timestamps are converted with the IANA `Europe/Paris` timezone before deriving the wall-clock hour and meteorological season: winter (Dec–Feb), spring (Mar–May), summer (Jun–Aug), autumn (Sep–Nov). This correctly skips hour 02 during the spring transition and maps both distinct autumn UTC instants to repeated local hour 02.

For each segment, pandas' deterministic linear interpolation computes:

- consumption 85th percentile;
- CO₂ 25th and 75th percentiles;
- renewable-share 75th percentile;
- fossil-share 75th percentile.

Only complete rows across these four measures enter calibration. The artifact records the exact UTC source period, complete-row count, generation timestamp, quantile method, minimum sample, fixed thresholds, precedence, and every segment's sample and thresholds. Pass `--generated-at` when byte-reproducible metadata is required.

## Fallback and precedence

The first segment meeting `min_sample` is used in this documented order:

1. season + local hour;
2. season;
3. local hour;
4. global history;
5. explicit fixed thresholds, only if every calibrated level is undersized or absent.

Classification precedence preserves the existing labels and order:

1. **Carbon-heavy**: CO₂ or fossil share reaches its upper threshold;
2. **Tense**: consumption reaches its 85th percentile;
3. **Renewable-rich**: renewable share reaches its upper threshold and CO₂ reaches its lower threshold;
4. **Calm**: none of the above.

Every classification returns `mood`, `reason`, `thresholds`, `segment`, `sample`, and `fallback`.

## CLI

Preferred, partition-pruned usage:

```powershell
.\.venv\Scripts\python.exe scripts\calibrate_mood.py --store-root data\processed\partitions --start 2024-01-01 --end 2026-01-01 --min-sample 30
```

For one-file interoperability during migration, use `--input file.parquet`. The default artifact is `data/processed/mood_calibration.json`.

## Official data references

- ODRÉ API dataset: `https://odre.opendatasoft.com/explore/dataset/eco2mix-national-tr/`
- API records endpoint: `https://odre.opendatasoft.com/api/explore/v2.1/catalog/datasets/eco2mix-national-tr/records`
- RTE éCO2mix description: `https://www.rte-france.com/eco2mix`

The offline smoke test uses the locally cached processed official response and never contacts the network.
