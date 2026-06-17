# Energy Pulse France

Energy Pulse France is a Python-first Streamlit application for trustworthy French electricity analysis. It preserves official raw payloads, standardizes RTE éCO2mix observations, maintains idempotent Parquet partitions, joins population-weighted weather, reports data quality, evaluates transparent demand baselines, and calibrates an educational grid-mood indicator.

No live, historical, weather, or forecast values are fabricated. Cached values are labelled as cached, and baseline forecasts are explicitly not described as AI.

## Official data sources

| Data | Access | Dataset / reference | Notes |
|---|---|---|---|
| Near-live national electricity | [ODRÉ/RTE éCO2mix](https://odre.opendatasoft.com/explore/dataset/eco2mix-national-tr/) | `eco2mix-national-tr`, Opendatasoft Explore API v2.1 | Public, no key; nominal 15-minute observations; rolling coverage and publication latency apply. |
| Consolidated national history | [ODRÉ/RTE consolidated éCO2mix](https://odre.opendatasoft.com/explore/dataset/eco2mix-national-cons-def/) | `eco2mix-national-cons-def`, Opendatasoft Explore API v2.1 | Public, Licence Ouverte 2.0; coverage starts in 2012; historical cadence/schema can differ from near-live data. |
| Weather | [Open-Meteo Forecast API](https://open-meteo.com/en/docs) and [Historical Weather API](https://open-meteo.com/en/docs/historical-weather-api) | Hourly temperature, wind, cloud cover, shortwave radiation, humidity | Public, no key; requested in UTC and cached by city/date. |
| City weights | [INSEE legal populations 2022](https://www.insee.fr/fr/statistiques/8290591) | Municipal population, ten major metropolitan-France communes | An auditable urban-demand proxy, not a complete national population model. |

Detailed source contracts and limitations are in `docs/`.

## Install

Python 3.11 or newer is recommended.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

No credential is needed for RTE/ODRÉ or Open-Meteo. `ENTSOE_API_TOKEN` remains optional and is not used by this pipeline.

## Reproduce the pipeline

Run commands from the repository root.

```powershell
# 1. Consolidated official history; writes an immutable raw snapshot,
#    a compatibility export, and year/month Parquet partitions.
python -m scripts.fetch_historical --start 2024-01-01 --end 2024-02-01

# 2. Incremental near-live update. Repeating it is idempotent.
python -m scripts.update_data --hours 72
python -m scripts.update_data --hours 72

# A bounded historical update can also use the unified command.
python -m scripts.update_data --start 2024-01-01 --end 2024-02-01

# 3. Multi-city weather plus energy/weather joined Parquet output.
python -m scripts.fetch_weather --start 2024-01-01 --end 2024-01-31

# 4–7. Quality, baselines, and calibrated mood artifacts.
python -m scripts.run_quality_checks
python -m scripts.backtest_baselines
python -m scripts.calibrate_mood

# 8. Dashboard.
python run_app.py
```

Use `python -m scripts.update_data --offline` to process the newest immutable near-live cache without a network call. Add `--strict` to the weather command when any missing city should fail the run.

## Integrated architecture

- `data/raw/`: immutable or source-faithful JSON caches with query and retrieval provenance; ignored by Git.
- `data/processed/eco2mix/year=YYYY/month=MM/data.parquet`: atomic, idempotent processed partitions keyed by UTC timestamp and region.
- `data/processed/weather_national.parquet`: population-weighted weather with source timestamps and coverage diagnostics.
- `data/processed/energy_weather.parquet`: weather joined backward to the electricity timeline, preventing future-data leakage.
- `data/processed/baseline_backtest.json`: deterministic prediction-level and metric artifact.
- `data/processed/mood_calibration.json`: quantile thresholds, source period, sample sizes, fallback metadata, and generation time.

The standardized electricity schema includes explicit UTC timestamps, region, consumption, generation by source, imports/exports, source CO₂ intensity, total/renewable/fossil production, and renewable/fossil shares. Suspicious records remain available as quality evidence; quality checks do not silently clean them away.

## Data quality

The report classifies required-schema, timestamp, duplicate, cadence, missing-interval, null, nonnegative-generation, share-range, freshness, extreme-value, and supply/demand residual checks as errors, warnings, or information. Defaults are screening thresholds, not corrections or RTE operational limits.

```powershell
python -m scripts.run_quality_checks --start 2024-01-01 --end 2024-02-01
```

## Forecast baselines

The backtest uses chronological rolling origins and exact target timestamps for persistence, previous-day, and previous-week seasonal-naive forecasts at 1, 3, 6, and 24 hours. It reports MAE, RMSE, sMAPE, sample count, missing targets, and coverage. These are reference rules that a future ML model must beat—not an AI model or production-quality forecast.

## Grid mood

Calibration uses Europe/Paris local hour and meteorological season. Transparent historical quantiles define high demand, high/low CO₂, high renewable share, and high fossil share. The fallback order is season/hour, season, hour, global, then explicit fixed thresholds. Decision precedence is Carbon-heavy, Tense, Renewable-rich, Calm. The result exposes its reason, thresholds, segment, sample count, and fallback status.

Grid mood is an educational indicator, not an RTE operational alert or grid-security assessment.

## Test

```powershell
python -m compileall -q app src scripts
python -m pytest -q
```

Real-service smoke tests are opt-in and skip gracefully offline:

```powershell
$env:RUN_REAL_DATA_TESTS="1"
$env:RUN_LIVE_DATA_SMOKE="1"
python -m pytest -q
```

## Known limitations

- ODRÉ and Open-Meteo availability and publication latency are external dependencies; raw caches support explicit offline fallback.
- The consolidated series can differ from the near-live schema and cadence. Quality reports expose cadence gaps; baseline coverage records unavailable exact targets.
- Population weighting covers ten large communes and is an urban exposure proxy, not all residents or regional weather diversity.
- Hourly weather is aligned backward to quarter-hours. It is leakage-safe but does not create new intra-hour observations.
- Source CO₂ intensity is retained; imported electricity is not decomposed by foreign generation mix.
- Multi-partition writes are atomic per month, not as one cross-month transaction.

## Next AI tasks

1. Train weather-aware gradient-boosted demand models only after defining chronological train/validation/test periods and beating every baseline by horizon.
2. Add probabilistic intervals and calibration diagnostics, especially around holidays and extreme weather.
3. Add regional éCO2mix and weather features with hierarchical validation.
4. Explain validated model predictions with time-safe SHAP analysis and plain-language driver summaries.
5. Monitor schema drift, forecast drift, and segment-level errors before any operational pilot.
