"""Small executable exploration proving the processed dataset can be loaded."""
from pathlib import Path

import pandas as pd

DATA = Path(__file__).resolve().parents[1] / "data" / "processed" / "eco2mix_latest.parquet"
frame = pd.read_parquet(DATA)
print(frame.info())
print(frame[["timestamp", "consumption_mw", "renewable_share", "co2_intensity_g_per_kwh"]].tail())
print("\nMissing values (%):\n", frame.isna().mean().mul(100).round(1).sort_values(ascending=False).head(10))

