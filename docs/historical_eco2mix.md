# Consolidated historical éCO2mix

## Official source

This adapter targets ODRÉ dataset `eco2mix-national-cons-def`, **éCO2mix – Données
consolidées et définitives nationales**, published by RTE on the Réseaux Énergies
open-data portal. The authoritative machine-readable endpoints are:

- catalog metadata: `https://odre.opendatasoft.com/api/explore/v2.1/catalog/datasets/eco2mix-national-cons-def`
- records: `https://odre.opendatasoft.com/api/explore/v2.1/catalog/datasets/eco2mix-national-cons-def/records`
- portal page: `https://odre.opendatasoft.com/explore/dataset/eco2mix-national-cons-def/`

The series starts on 2012-01-01 and is normally half-hourly. RTE consolidates the
values after publication; consumers should read the current catalog metadata for
its update timestamp and exact current coverage. The portal metadata is also the
authority for the reuse license (Open Licence / Licence Ouverte 2.0). The API v2.1
records endpoint accepts at most 100 records per page and deep offsets are limited,
so this adapter uses seven-day chunks plus pagination rather than relying on a
single large offset. Opendatasoft v2.1 caps `limit` at 100 and `offset` at
10,000; seven-day chunks keep this feed well below that offset ceiling.

Network access to both official ODRÉ hostnames timed out in the coding sandbox on
2026-06-17. No response was substituted or fabricated. The URLs, dataset identity,
and expected schema are implemented explicitly; run the opt-in smoke test below in
a network-enabled environment to re-verify the live service and record coverage.

Expected core fields are `date_heure`, `consommation`, `nucleaire`, `eolien`,
`solaire`, `hydraulique`, `gaz`, `charbon`, `fioul`, `bioenergies`,
`ech_physiques`, and `taux_co2`. Field-label variants from downloaded CSV files
(including accented labels) are reconciled before passing data to the existing
`clean_energy_mix` contract.

## Usage

Dates form a UTC half-open interval: start is inclusive and end is exclusive.
Requests are capped at 366 days. Adjacent requests can therefore be concatenated
without retaining duplicate boundary rows.

```powershell
python scripts/fetch_historical.py --start 2024-01-01 --end 2024-02-01
python scripts/fetch_historical.py --start 2024-01-01 --end 2024-01-08 --output data/processed/january_week.csv
```

Raw responses are stored under `data/raw/rte_eco2mix_historical`. Each JSON file
contains source URL, retrieval time, exact interval, record count, and SHA-256.
Its name is content-addressed and creation is exclusive: an identical snapshot is
never overwritten. Standardized Parquet/CSV output remains a thin boundary over
the project's existing clean-energy DataFrame contract.

The Streamlit page `app/pages/4_historical.py` loads the latest cache without a
network request, supports bounded date fetching, demand and production charts,
summary metrics, and standardized CSV download.

## Verification

Mocked tests cover pagination, half-open boundaries, deduplication, schema aliases,
empty results, invalid ranges, malformed API responses, and immutable cache files.
The real-data test is deliberately offline-safe:

```powershell
pytest -q tests/test_historical_eco2mix.py
$env:RUN_REAL_DATA_TESTS="1"; pytest -q tests/test_historical_eco2mix.py -k real
```

The second command fetches a two-hour interval directly from official ODRÉ and
skips by default when the environment variable is absent.
