from __future__ import annotations

import pandas as pd


def require_columns(frame: pd.DataFrame, columns: set[str], context: str = "data") -> None:
    missing = columns.difference(frame.columns)
    if missing:
        raise ValueError(f"{context} is missing required columns: {sorted(missing)}")


def require_non_empty(frame: pd.DataFrame, context: str = "data") -> None:
    if frame.empty:
        raise ValueError(f"{context} is empty")

