from __future__ import annotations

import pandas as pd

from src.data_processing.validation import require_columns, require_non_empty

SOURCE_MAP = {
    "consommation": "consumption_mw",
    "nucleaire": "nuclear_mw",
    "eolien": "wind_mw",
    "solaire": "solar_mw",
    "hydraulique": "hydro_mw",
    "gaz": "gas_mw",
    "charbon": "coal_mw",
    "fioul": "oil_mw",
    "bioenergies": "bioenergy_mw",
    "taux_co2": "co2_intensity_g_per_kwh",
    "ech_physiques": "net_imports_mw",
}
OPTIONAL_SOURCE_MAP = {
    "prevision_j": "rte_forecast_j_mw",
    "prevision_j1": "rte_forecast_j1_mw",
}
NUMERIC_COLUMNS = list(SOURCE_MAP.values())


def clean_energy_mix(raw: pd.DataFrame) -> pd.DataFrame:
    require_non_empty(raw, "raw éCO2mix data")
    require_columns(raw, {"date_heure", *SOURCE_MAP.keys()}, "raw éCO2mix data")

    frame = raw.rename(columns=SOURCE_MAP).copy()
    frame = frame.rename(columns={key: value for key, value in OPTIONAL_SOURCE_MAP.items() if key in frame})
    frame["timestamp"] = pd.to_datetime(frame["date_heure"], utc=True, errors="coerce")
    frame["region"] = frame.get("perimetre", "France").fillna("France")
    for column in NUMERIC_COLUMNS:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    for column in OPTIONAL_SOURCE_MAP.values():
        if column in frame:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")

    frame = frame.dropna(subset=["timestamp", "consumption_mw"]).sort_values("timestamp")
    frame = frame.drop_duplicates(subset=["timestamp", "region"], keep="last")
    frame["imports_mw"] = frame["net_imports_mw"].clip(lower=0)
    frame["exports_mw"] = (-frame["net_imports_mw"]).clip(lower=0)

    renewable = ["wind_mw", "solar_mw", "hydro_mw", "bioenergy_mw"]
    fossil = ["gas_mw", "coal_mw", "oil_mw"]
    production = ["nuclear_mw", *renewable, *fossil]
    frame["renewable_production_mw"] = frame[renewable].sum(axis=1, min_count=1)
    frame["fossil_production_mw"] = frame[fossil].sum(axis=1, min_count=1)
    frame["total_production_mw"] = frame[production].sum(axis=1, min_count=1)
    denominator = frame["total_production_mw"].where(frame["total_production_mw"] > 0)
    frame["renewable_share"] = frame["renewable_production_mw"] / denominator
    frame["fossil_share"] = frame["fossil_production_mw"] / denominator

    columns = [
        "timestamp", "region", "consumption_mw", "nuclear_mw", "wind_mw",
        "solar_mw", "hydro_mw", "gas_mw", "coal_mw", "oil_mw",
        "bioenergy_mw", "imports_mw", "exports_mw", "net_imports_mw",
        "co2_intensity_g_per_kwh", "total_production_mw",
        "renewable_production_mw", "renewable_share", "fossil_production_mw",
        "fossil_share",
    ]
    columns.extend(column for column in OPTIONAL_SOURCE_MAP.values() if column in frame)
    return frame[columns].reset_index(drop=True)


def grid_mood(frame: pd.DataFrame) -> tuple[str, str]:
    """Temporary transparent thresholds; replace with calibrated logic later."""
    require_non_empty(frame, "clean energy data")
    latest = frame.sort_values("timestamp").iloc[-1]
    high_demand = frame["consumption_mw"].quantile(0.85)
    if latest["co2_intensity_g_per_kwh"] >= 80 or latest["fossil_share"] >= 0.15:
        return "Carbon-heavy", "CO₂ ≥ 80 g/kWh or fossil share ≥ 15%"
    if latest["consumption_mw"] >= high_demand and len(frame) >= 8:
        return "Tense", "Demand is in the top 15% of the loaded period"
    if latest["renewable_share"] >= 0.35 and latest["co2_intensity_g_per_kwh"] <= 40:
        return "Renewable-rich", "Renewables ≥ 35% and CO₂ ≤ 40 g/kWh"
    return "Calm", "No temporary stress or carbon threshold is triggered"
