# National multi-city weather

This pipeline fetches hourly weather for ten major metropolitan-French communes, keeps every city observation, aligns it backward to energy quarter-hours in UTC, and then calculates population-weighted national context. Backward as-of alignment has a 59-minute tolerance: a 10:45 energy row may use 10:00 weather, but never 11:00 weather.

## Sources and reference

- Weather: [Open-Meteo Historical Weather API](https://open-meteo.com/en/docs/historical-weather-api) (`archive-api.open-meteo.com/v1/archive`) and [Forecast API](https://open-meteo.com/en/docs) (`api.open-meteo.com/v1/forecast`). Requested hourly fields are `temperature_2m`, `wind_speed_10m`, `cloud_cover`, `shortwave_radiation`, and `relative_humidity_2m`, with `timezone=UTC`.
- Population: [INSEE, populations légales 2022](https://www.insee.fr/fr/statistiques/8290591), population municipale by commune, reference date 1 January 2022. The source archive is linked in `fr_major_cities_v1.json`; INSEE commune codes make every row auditable.

The versioned reference is `src/data_sources/fr_major_cities_v1.json`. It deliberately represents urban population exposure rather than national land area. Its ten communes and weights must not be described as a complete France-wide population average.

## Fetch and cache

```powershell
python scripts/fetch_weather.py --start 2024-01-15 --end 2024-01-15
```

Raw responses are cached independently under `data/raw/weather_national/open_meteo_<city>_<start>_<end>.json`. Re-running an identical request is offline-safe and makes no HTTP call. Use `--force-refresh` to replace matching cache entries, `--cities paris lyon` for a small sample, and `--strict` when partial city success is unacceptable. Historical dates use the Archive API; current/future ranges use Forecast.

The generated table includes weighted weather fields plus `weather_population_coverage`, available/expected city counts, `weather_missing_cities`, and `weather_source_timestamp_max`. Available-city weights are renormalized, while coverage retains the missing-population signal.

## Energy integration

Call `build_national_weather_features(raw_weather, energy["timestamp"])`, then `join_energy_weather(energy, features)`. Both timestamps and provenance are UTC. The join rejects any row whose weather source timestamp is later than its energy timestamp. This avoids DST ambiguity and future leakage.

`app/components/weather_context.py` exposes `render_weather_context(features)` for a dashboard page. The lead should call it from the desired page after loading/joining weather; shared `app/main.py` is intentionally unchanged.

## Validation and current fetch status

Focused tests cover reference weights, exact weighted calculations, missing-city coverage, deterministic cache reuse without network, Archive/UTC parameters, DST fallback, backward alignment, and future-provenance rejection. At implementation time, direct outbound requests from the coding environment timed out even after network approval; therefore no response is claimed as fetched until a subsequent successful smoke run is recorded here or in the handoff report.
