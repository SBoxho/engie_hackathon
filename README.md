# Energy Pulse France

Energy Pulse France is a Python-first Streamlit application for trustworthy French electricity analysis. It preserves official raw payloads, standardizes RTE éCO2mix observations, maintains idempotent Parquet partitions, joins population-weighted weather, reports data quality, evaluates transparent demand baselines, and calibrates an educational grid-mood indicator.

No live, historical, weather, or forecast values are fabricated. Cached values are labelled as cached, and baseline forecasts are explicitly not described as AI.

## UX structure

The Streamlit entry page is now a story-first public demo: a hero, current grid pulse cards, a 24-hour demand-pressure timeline, plain-language driver cards, a demand-shifting simulator link, and a model-honesty box. Raw dataframes, calibration details, data quality checks, historical views, baseline backtests, and the experimental demand model remain available from the **Advanced / Data Science** section so non-technical reviewers see the energy-weather story first while technical reviewers can still inspect the evidence.

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

# 8. Experimental weather-aware demand model.
python -m scripts.build_features
python -m scripts.train_demand_model
python -m scripts.evaluate_demand_model

# 9. Dashboard.
python run_app.py
```

Use `python -m scripts.update_data --offline` to process the newest immutable near-live cache without a network call. Add `--strict` to the weather command when any missing city should fail the run.

## Integrated architecture

- `data/raw/`: immutable or source-faithful JSON caches with query and retrieval provenance; ignored by Git.
- `data/processed/eco2mix/year=YYYY/month=MM/data.parquet`: atomic, idempotent processed partitions keyed by UTC timestamp and region.
- `data/processed/weather_national.parquet`: population-weighted weather with source timestamps and coverage diagnostics.
- `data/processed/energy_weather.parquet`: weather joined backward to the electricity timeline, preventing future-data leakage.
- `data/processed/baseline_backtest.json`: deterministic prediction-level and metric artifact.
- `data/processed/demand_model/features.parquet`: supervised demand-model features generated from exact UTC forecast origins; ignored by Git.
- `data/processed/demand_model/feature_metadata.json`: feature schema, leakage controls, source coverage, and weather coverage.
- `data/processed/demand_model/demand_hgb_model.pkl`: generated scikit-learn model bundle; ignored by Git.
- `data/processed/demand_model/evaluation.json`: model-versus-baseline metrics and prediction records for untouched chronological test periods.
- `data/processed/mood_calibration.json`: quantile thresholds, source period, sample sizes, fallback metadata, and generation time.

The standardized electricity schema includes explicit UTC timestamps, region, consumption, generation by source, imports/exports, source CO₂ intensity, total/renewable/fossil production, and renewable/fossil shares. Suspicious records remain available as quality evidence; quality checks do not silently clean them away.

## Data quality

The report classifies required-schema, timestamp, duplicate, cadence, missing-interval, null, nonnegative-generation, share-range, freshness, extreme-value, and supply/demand residual checks as errors, warnings, or information. Defaults are screening thresholds, not corrections or RTE operational limits.

```powershell
python -m scripts.run_quality_checks --start 2024-01-01 --end 2024-02-01
```

## Forecast baselines

The backtest uses chronological rolling origins and exact target timestamps for persistence, previous-day, and previous-week seasonal-naive forecasts at 1, 3, 6, and 24 hours. It reports MAE, RMSE, sMAPE, sample count, missing targets, and coverage. These are reference rules that a future ML model must beat—not an AI model or production-quality forecast.

## Experimental demand model

The demand model is a CPU-friendly scikit-learn `HistGradientBoostingRegressor`, trained as one direct model per 1, 3, 6, and 24 hour horizon. It uses only features available at the forecast origin: observed demand lags, shifted rolling demand statistics, Europe/Paris calendar features across DST, holiday flags, and population-weighted weather joined with source provenance no later than the origin. Target demand is never interpolated.

```powershell
python -m scripts.build_features --min-continuous-hours 48
python -m scripts.train_demand_model
python -m scripts.evaluate_demand_model
```

Feature generation infers the observed demand cadence, so consolidated 30-minute history and near-live 15-minute history are both handled without target interpolation. The evaluator uses chronological train/test splits and compares the model against persistence, previous-day, and previous-week baselines on the exact same test origins. The Streamlit **Demand model** page shows recent actuals versus model and baseline predictions, horizon metrics, training/evaluation periods, data freshness, weather coverage, and artifact timestamps. The page labels the model as experimental and not an RTE operational forecast.

To backfill a continuous multi-season training set, use a bounded consolidated demand window and the matching historical weather window:

```powershell
python -m scripts.fetch_historical --start 2024-01-01 --end 2025-01-01 --output data/processed/eco2mix_historical_2024_clean.parquet
python -m scripts.fetch_weather --start 2024-01-01 --end 2024-12-31 --output data/processed/weather_national_2024.parquet --joined-output data/processed/energy_weather_2024.parquet --strict
python -m scripts.build_features --start 2024-01-01 --end 2025-01-01 --weather data/processed/weather_national_2024.parquet --min-continuous-hours 168
python -m scripts.train_demand_model
python -m scripts.evaluate_demand_model
python -m scripts.backtest_baselines --start 2024-01-01 --end 2025-01-01
```

See `docs/demand_model.md` for features, split assumptions, artifact format, limitations, and interpretation guidance. Model performance depends on obtaining a sufficiently long, continuous historical demand and weather dataset; short cached slices can train a smoke-test model but must not be read as evidence of forecasting skill.

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
- The experimental demand model is only as good as the continuous history available. It may correctly report that baselines are stronger, that week-naive baselines are ineligible, or that there is insufficient data to train.
- Source CO₂ intensity is retained; imported electricity is not decomposed by foreign generation mix.
- Multi-partition writes are atomic per month, not as one cross-month transaction.

## Next AI tasks

1. Extend the continuous official history and weather backfill, then retrain until the model is evaluated over multiple seasons and holidays.
2. Add probabilistic intervals and calibration diagnostics, especially around holidays and extreme weather.
3. Add regional éCO2mix and weather features with hierarchical validation.
4. Explain validated model predictions with time-safe SHAP analysis and plain-language driver summaries.
5. Monitor schema drift, forecast drift, and segment-level errors before any operational pilot.
