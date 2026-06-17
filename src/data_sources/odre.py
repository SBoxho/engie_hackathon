"""Small generic adapter for public ODRÉ Opendatasoft datasets."""
from __future__ import annotations

import pandas as pd
import requests

from src.config import settings


def fetch_dataset(dataset_id: str, *, limit: int = 100, where: str | None = None) -> pd.DataFrame:
    if not dataset_id.replace("-", "").isalnum():
        raise ValueError("Invalid ODRÉ dataset identifier")
    url = f"{settings.odre_base_url}/catalog/datasets/{dataset_id}/records"
    params: dict[str, str | int] = {"limit": min(max(limit, 1), 100)}
    if where:
        params["where"] = where
    response = requests.get(url, params=params, timeout=30)
    response.raise_for_status()
    records = response.json().get("results", [])
    if not records:
        raise ValueError(f"ODRÉ dataset {dataset_id!r} returned no records")
    return pd.DataFrame.from_records(records)

