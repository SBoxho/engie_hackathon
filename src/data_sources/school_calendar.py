"""Official French school holiday calendar from data.education.gouv.fr."""
from __future__ import annotations

import hashlib
import json
import unicodedata
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from src.config import settings
from src.utils.io import latest_file, read_json


DATASET_ID = "fr-en-calendrier-scolaire"
DATASET_TITLE = "Calendrier scolaire"
BASE_URL = "https://data.education.gouv.fr/api/explore/v2.1"
API_PAGE_SIZE = 100
ZONE_COLUMNS = ("a", "b", "c")

ALIASES = {
    "start_date": "start_date",
    "date_de_debut": "start_date",
    "debut": "start_date",
    "end_date": "end_date",
    "date_de_fin": "end_date",
    "fin": "end_date",
    "zones": "zones",
    "zone": "zones",
    "location": "location",
    "lieu": "location",
    "description": "description",
    "population": "population",
    "annee_scolaire": "school_year",
}


class SchoolCalendarError(RuntimeError):
    """Raised when the official school calendar cannot be reconciled."""


def records_url(base_url: str = BASE_URL) -> str:
    return f"{base_url.rstrip('/')}/catalog/datasets/{DATASET_ID}/records"


def _date(value: str | date | datetime) -> date:
    return pd.Timestamp(value).date()


def _field_id(column: object) -> str:
    text = unicodedata.normalize("NFKD", str(column)).encode("ascii", "ignore").decode()
    return "_".join("".join(ch if ch.isalnum() else " " for ch in text.lower()).split())


def _cache_snapshot(
    records: list[dict[str, Any]], start: date, end: date, cache_dir: Path
) -> Path:
    canonical = json.dumps(records, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    content_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    query_key = f"{start.isoformat()}_{end.isoformat()}"
    query_sha = hashlib.sha256(query_key.encode("ascii")).hexdigest()[:12]
    path = cache_dir / f"{DATASET_ID}_{query_sha}_{content_sha256[:12]}.json"
    payload = {
        "provenance": {
            "dataset_id": DATASET_ID,
            "dataset_title": DATASET_TITLE,
            "source_url": records_url(),
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "interval": {
                "start_inclusive": start.isoformat(),
                "end_exclusive": end.isoformat(),
            },
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
        pass
    return path


def fetch_school_calendar_raw(
    start: str | date | datetime,
    end: str | date | datetime,
    *,
    cache: bool = True,
    cache_dir: Path | None = None,
    session: requests.Session | None = None,
    timeout: int = 30,
) -> pd.DataFrame:
    """Fetch official school calendar rows overlapping ``[start, end)`` dates."""
    start_date, end_date = _date(start), _date(end)
    if start_date >= end_date:
        raise ValueError("start must be earlier than end")
    client = session or requests.Session()
    records: list[dict[str, Any]] = []
    offset = 0
    while True:
        params = {
            "limit": API_PAGE_SIZE,
            "offset": offset,
            "where": (
                f'start_date < "{end_date.isoformat()}" AND '
                f'end_date >= "{start_date.isoformat()}"'
            ),
            "order_by": "start_date asc",
        }
        try:
            response = client.get(records_url(), params=params, timeout=timeout)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise SchoolCalendarError(f"failed to fetch school calendar: {exc}") from exc
        batch = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(batch, list):
            raise SchoolCalendarError("school calendar response has no valid 'results' array")
        records.extend(batch)
        total = int(payload.get("total_count", len(batch)))
        offset += len(batch)
        if not batch or offset >= total:
            break
    frame = pd.DataFrame.from_records(records)
    if cache:
        _cache_snapshot(
            frame.to_dict(orient="records"),
            start_date,
            end_date,
            cache_dir or settings.raw_dir / "school_calendar",
        )
    return frame


def _zones(value: object) -> tuple[str, ...]:
    if isinstance(value, list):
        text = " ".join(str(item) for item in value)
    else:
        text = "" if pd.isna(value) else str(value)
    normalised = _field_id(text).replace("_", " ")
    zones = []
    for zone in ZONE_COLUMNS:
        if f"zone {zone}" in normalised or normalised == zone:
            zones.append(zone)
    return tuple(zones or ZONE_COLUMNS)


def normalize_school_calendar(raw: pd.DataFrame) -> pd.DataFrame:
    """Return one row per official holiday interval and school zone."""
    if raw.empty:
        return pd.DataFrame(columns=["start_date", "end_date", "zone", "description", "source"])
    renamed = {column: ALIASES.get(_field_id(column), _field_id(column)) for column in raw.columns}
    frame = raw.rename(columns=renamed).copy()
    missing = {"start_date", "end_date"}.difference(frame.columns)
    if missing:
        raise SchoolCalendarError(f"school calendar is missing fields: {sorted(missing)}")
    frame["start_date"] = pd.to_datetime(frame["start_date"], errors="coerce").dt.date
    frame["end_date"] = pd.to_datetime(frame["end_date"], errors="coerce").dt.date
    frame = frame.dropna(subset=["start_date", "end_date"])
    if "population" in frame:
        population = frame["population"].astype(str).map(_field_id)
        pupil_rows = population.str.contains("eleve", na=False)
        if pupil_rows.any():
            frame = frame.loc[pupil_rows].copy()
    rows: list[dict[str, Any]] = []
    for row in frame.itertuples(index=False):
        zones = _zones(getattr(row, "zones", None))
        for zone in zones:
            rows.append(
                {
                    "start_date": row.start_date,
                    "end_date": row.end_date,
                    "zone": zone,
                    "description": str(getattr(row, "description", "") or ""),
                    "source": DATASET_ID,
                }
            )
    return (
        pd.DataFrame(rows)
        .drop_duplicates(["start_date", "end_date", "zone", "description"])
        .sort_values(["start_date", "zone", "end_date"], kind="stable")
        .reset_index(drop=True)
    )


def fetch_school_calendar(
    start: str | date | datetime,
    end: str | date | datetime,
    **kwargs: Any,
) -> pd.DataFrame:
    return normalize_school_calendar(fetch_school_calendar_raw(start, end, **kwargs))


def load_cached_school_calendar(
    path: Path | None = None, *, cache_dir: Path | None = None, clean: bool = True
) -> pd.DataFrame:
    directory = cache_dir or settings.raw_dir / "school_calendar"
    path = path or latest_file(directory, f"{DATASET_ID}_*.json")
    if path is None or not path.exists():
        raise FileNotFoundError("no cached school calendar snapshot found")
    payload = read_json(path)
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise SchoolCalendarError("invalid school calendar cache payload")
    frame = pd.DataFrame.from_records(payload["results"])
    return normalize_school_calendar(frame) if clean else frame


def school_holiday_features(local_times: pd.Series, calendar: pd.DataFrame | None) -> pd.DataFrame:
    """Build deterministic school-holiday flags for local timestamps."""
    index = local_times.index
    result = pd.DataFrame(index=index)
    for zone in ZONE_COLUMNS:
        result[f"school_holiday_zone_{zone}"] = 0
    result["school_holiday_any_zone"] = 0
    result["school_holiday_all_zones"] = 0
    if calendar is None or calendar.empty:
        return result

    required = {"start_date", "end_date", "zone"}
    missing = required - set(calendar.columns)
    if missing:
        raise SchoolCalendarError(f"school calendar features missing fields: {sorted(missing)}")

    lookup: dict[date, set[str]] = {}
    clean = normalize_school_calendar(calendar) if "source" not in calendar else calendar.copy()
    clean["start_date"] = pd.to_datetime(clean["start_date"], errors="coerce").dt.date
    clean["end_date"] = pd.to_datetime(clean["end_date"], errors="coerce").dt.date
    for row in clean.dropna(subset=["start_date", "end_date"]).itertuples(index=False):
        end = pd.Timestamp(row.end_date) - pd.Timedelta(days=1)
        for day in pd.date_range(pd.Timestamp(row.start_date), end, freq="D"):
            lookup.setdefault(day.date(), set()).add(str(row.zone).lower())

    local_dates = local_times.dt.date
    for idx, day in local_dates.items():
        zones = lookup.get(day, set())
        for zone in ZONE_COLUMNS:
            result.at[idx, f"school_holiday_zone_{zone}"] = int(zone in zones)
        result.at[idx, "school_holiday_any_zone"] = int(bool(zones))
        result.at[idx, "school_holiday_all_zones"] = int(all(zone in zones for zone in ZONE_COLUMNS))
    return result
