"""Consolidated national éCO2mix history from the official ODRÉ API.

The public API is queried in short chunks because Opendatasoft limits a page to
100 records and caps deep offsets. Public functions use a half-open [start, end)
UTC interval so adjacent exports can be concatenated without duplicate edges.
"""
from __future__ import annotations

import hashlib
import json
import unicodedata
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import pandas as pd
import requests

from src.config import settings
from src.data_processing.clean_energy_mix import clean_energy_mix
from src.utils.io import latest_file, read_json
from src.utils.logging import get_logger

LOGGER = get_logger(__name__)

DATASET_ID = "eco2mix-national-cons-def"
DATASET_TITLE = "éCO2mix - Données consolidées et définitives nationales"
API_PAGE_SIZE = 100
DEFAULT_CHUNK_DAYS = 7
MAX_RANGE_DAYS = 366
EARLIEST_AVAILABLE = datetime(2012, 1, 1, tzinfo=timezone.utc)

RAW_REQUIRED = {
    "date_heure",
    "consommation",
    "nucleaire",
    "eolien",
    "solaire",
    "hydraulique",
    "gaz",
    "charbon",
    "fioul",
    "bioenergies",
    "ech_physiques",
    "taux_co2",
}

# Historical exports have used both labels and field identifiers. Normalizing
# accents/punctuation first makes old downloaded snapshots usable as well.
ALIASES = {
    "date_heure": "date_heure",
    "date_et_heure": "date_heure",
    "consommation": "consommation",
    "nucleaire": "nucleaire",
    "eolien": "eolien",
    "solaire": "solaire",
    "hydraulique": "hydraulique",
    "gaz": "gaz",
    "charbon": "charbon",
    "fioul": "fioul",
    "bioenergies": "bioenergies",
    "ech_physiques": "ech_physiques",
    "echanges_physiques": "ech_physiques",
    "taux_co2": "taux_co2",
    "perimetre": "perimetre",
}


class HistoricalEco2MixError(RuntimeError):
    """Raised when historical éCO2mix cannot be fetched or reconciled."""


def records_url(base_url: str | None = None) -> str:
    base = (base_url or settings.odre_base_url).rstrip("/")
    return f"{base}/catalog/datasets/{DATASET_ID}/records"


def metadata_url(base_url: str | None = None) -> str:
    base = (base_url or settings.odre_base_url).rstrip("/")
    return f"{base}/catalog/datasets/{DATASET_ID}"


def _utc(value: str | date | datetime, *, end: bool = False) -> datetime:
    if isinstance(value, str):
        parsed = pd.Timestamp(value)
        value = parsed.to_pydatetime()
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, date):
        result = datetime.combine(value, time.min)
    else:
        raise TypeError("dates must be strings, date, or datetime instances")
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def validate_range(
    start: str | date | datetime, end: str | date | datetime
) -> tuple[datetime, datetime]:
    """Validate and convert a half-open [start, end) interval to UTC."""
    start_utc, end_utc = _utc(start), _utc(end, end=True)
    if start_utc < EARLIEST_AVAILABLE:
        raise ValueError("historical éCO2mix starts on 2012-01-01")
    if start_utc >= end_utc:
        raise ValueError("start must be earlier than end")
    if end_utc - start_utc > timedelta(days=MAX_RANGE_DAYS):
        raise ValueError(f"a request may span at most {MAX_RANGE_DAYS} days")
    return start_utc, end_utc


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _chunks(start: datetime, end: datetime, days: int) -> Iterator[tuple[datetime, datetime]]:
    cursor = start
    step = timedelta(days=days)
    while cursor < end:
        boundary = min(cursor + step, end)
        yield cursor, boundary
        cursor = boundary


def _cache_snapshot(
    records: list[dict[str, Any]], start: datetime, end: datetime, cache_dir: Path
) -> Path:
    canonical = json.dumps(
        records, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    content_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    query_key = f"{_iso(start)}_{_iso(end)}"
    query_sha = hashlib.sha256(query_key.encode("ascii")).hexdigest()[:12]
    path = cache_dir / f"{DATASET_ID}_{query_sha}_{content_sha256[:12]}.json"
    payload = {
        "provenance": {
            "dataset_id": DATASET_ID,
            "dataset_title": DATASET_TITLE,
            "source_url": records_url(),
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "interval": {"start_inclusive": _iso(start), "end_exclusive": _iso(end)},
            "record_count": len(records),
            "content_sha256": content_sha256,
        },
        "results": records,
    }
    cache_dir.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
    except FileExistsError:
        pass  # Identical query and content: preserve the original retrieval time.
    return path


def fetch_dataset_metadata(
    *, session: requests.Session | None = None, timeout: int = 30
) -> dict[str, Any]:
    """Fetch the authoritative current catalog metadata for provenance checks."""
    client = session or requests.Session()
    try:
        response = client.get(metadata_url(), timeout=timeout)
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise HistoricalEco2MixError(f"failed to fetch ODRÉ metadata: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("dataset_id", DATASET_ID) != DATASET_ID:
        raise HistoricalEco2MixError("ODRÉ returned invalid dataset metadata")
    return payload


def fetch_historical_raw(
    start: str | date | datetime,
    end: str | date | datetime,
    *,
    cache: bool = True,
    cache_dir: Path | None = None,
    session: requests.Session | None = None,
    timeout: int = 30,
    chunk_days: int = DEFAULT_CHUNK_DAYS,
) -> pd.DataFrame:
    """Fetch raw consolidated records in a bounded half-open UTC interval."""
    start_utc, end_utc = validate_range(start, end)
    if not 1 <= chunk_days <= 31:
        raise ValueError("chunk_days must be between 1 and 31")
    client = session or requests.Session()
    records: list[dict[str, Any]] = []

    for chunk_start, chunk_end in _chunks(start_utc, end_utc, chunk_days):
        offset = 0
        while True:
            params = {
                "limit": API_PAGE_SIZE,
                "offset": offset,
                "where": (
                    f'date_heure >= "{_iso(chunk_start)}" AND '
                    f'date_heure < "{_iso(chunk_end)}"'
                ),
                "order_by": "date_heure asc",
            }
            try:
                response = client.get(records_url(), params=params, timeout=timeout)
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as exc:
                raise HistoricalEco2MixError(f"failed to fetch historical éCO2mix: {exc}") from exc
            batch = payload.get("results") if isinstance(payload, dict) else None
            if not isinstance(batch, list):
                raise HistoricalEco2MixError("ODRÉ response has no valid 'results' array")
            records.extend(batch)
            total = int(payload.get("total_count", len(batch)))
            offset += len(batch)
            if not batch or offset >= total:
                break

    # Boundary filtering and deduplication protect against API overlap and
    # inconsistent timezone serialization in old records.
    frame = pd.DataFrame.from_records(records)
    if not frame.empty:
        if "date_heure" not in frame:
            raise HistoricalEco2MixError("historical records are missing date_heure")
        timestamps = pd.to_datetime(frame["date_heure"], utc=True, errors="coerce")
        frame = frame.loc[timestamps.ge(start_utc) & timestamps.lt(end_utc)].copy()
        frame["date_heure"] = timestamps.loc[frame.index]
        frame = frame.sort_values("date_heure").drop_duplicates("date_heure", keep="last")
        frame = frame.reset_index(drop=True)
    if cache:
        target = cache_dir or settings.raw_dir / "rte_eco2mix_historical"
        path = _cache_snapshot(frame.to_dict(orient="records"), start_utc, end_utc, target)
        LOGGER.info("Cached %s historical éCO2mix records at %s", len(frame), path)
    return frame


def _field_id(column: object) -> str:
    text = unicodedata.normalize("NFKD", str(column)).encode("ascii", "ignore").decode()
    return "_".join("".join(character if character.isalnum() else " " for character in text.lower()).split())


def reconcile_historical_schema(raw: pd.DataFrame) -> pd.DataFrame:
    """Reconcile historical field variants to the live clean-energy input."""
    if raw.empty:
        return pd.DataFrame(columns=sorted(RAW_REQUIRED | {"perimetre"}))
    renamed = {column: ALIASES.get(_field_id(column), _field_id(column)) for column in raw.columns}
    frame = raw.rename(columns=renamed).copy()
    missing_essential = {"date_heure", "consommation"}.difference(frame.columns)
    if missing_essential:
        raise HistoricalEco2MixError(
            f"historical schema is missing essential fields: {sorted(missing_essential)}"
        )
    for column in RAW_REQUIRED.difference(frame.columns):
        frame[column] = pd.NA
    if "perimetre" not in frame:
        frame["perimetre"] = "France"
    return frame


def to_clean_energy_mix(raw: pd.DataFrame) -> pd.DataFrame:
    """Return the project's standardized clean-energy DataFrame contract."""
    reconciled = reconcile_historical_schema(raw)
    if reconciled.empty:
        # Derive contract columns from the existing standardizer without
        # introducing a second storage model.
        template = {column: 0 for column in RAW_REQUIRED}
        template.update({"date_heure": "2012-01-01T00:00:00Z", "perimetre": "France"})
        return clean_energy_mix(pd.DataFrame([template])).iloc[0:0]
    return clean_energy_mix(reconciled)


def fetch_historical(
    start: str | date | datetime, end: str | date | datetime, **kwargs: Any
) -> pd.DataFrame:
    """Fetch and standardize consolidated national éCO2mix history."""
    return to_clean_energy_mix(fetch_historical_raw(start, end, **kwargs))


def load_cached_historical(
    path: Path | None = None, *, cache_dir: Path | None = None, clean: bool = True
) -> pd.DataFrame:
    directory = cache_dir or settings.raw_dir / "rte_eco2mix_historical"
    path = path or latest_file(directory, f"{DATASET_ID}_*.json")
    if path is None or not path.exists():
        raise FileNotFoundError("no cached historical éCO2mix snapshot found")
    payload = read_json(path)
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise HistoricalEco2MixError("invalid historical cache payload")
    frame = pd.DataFrame.from_records(payload["results"])
    return to_clean_energy_mix(frame) if clean else frame
