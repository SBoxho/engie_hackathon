from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.components.cards import metric_card
from app.components.charts import consumption_chart, mix_donut, production_area_chart
from app.components.data_quality import render_data_quality
from app.components.layout import apply_theme
from app.components.mood_explanation import render_mood_explanation
from app.components.weather_context import render_weather_context
from src.config import settings
from src.data_processing.clean_energy_mix import clean_energy_mix
from src.data_processing.features import add_time_features
from src.data_processing.storage import PartitionedParquetStore
from src.data_processing.weather_features import join_energy_weather
from src.data_sources.rte_eco2mix import Eco2MixError, fetch_eco2mix, load_cached_eco2mix
from src.models.mood_calibration import FIXED_THRESHOLDS, classify_mood

st.set_page_config(page_title="Energy Pulse France", page_icon="⚡", layout="wide")
apply_theme()


@st.cache_data(ttl=900, show_spinner=False)
def load_data(hours: int) -> tuple[pd.DataFrame, str]:
    """Prefer a fresh official observation, then explicit local fallbacks."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    try:
        raw = fetch_eco2mix(start=start, end=end)
        clean = add_time_features(clean_energy_mix(raw), settings.timezone)
        PartitionedParquetStore(settings.energy_store_dir).upsert(clean)
        return clean, "Official RTE éCO2mix · refreshed from ODRÉ"
    except (Eco2MixError, OSError):
        stored = PartitionedParquetStore(settings.energy_store_dir).read(start=start, end=end)
        if not stored.empty:
            return stored, "Official RTE éCO2mix · local partitioned snapshot"
        raw = load_cached_eco2mix()
        return clean_energy_mix(raw), "Official RTE éCO2mix · cached raw snapshot"


def mood_artifact() -> tuple[dict, str]:
    if settings.mood_artifact_path.exists():
        return json.loads(settings.mood_artifact_path.read_text(encoding="utf-8")), "calibrated"
    return {
        "timezone": settings.timezone,
        "min_sample": 1,
        "segments": [],
        "fixed_thresholds": FIXED_THRESHOLDS,
        "precedence": ["Carbon-heavy", "Tense", "Renewable-rich", "Calm"],
        "source": {"name": "Explicit fixed-threshold fallback"},
        "generated_at": None,
    }, "fixed fallback"


st.markdown('<div class="eyebrow">France · electricity system</div>', unsafe_allow_html=True)
st.markdown('<div class="hero">Energy Pulse France</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="subtitle">Trustworthy data engineering and honest forecasting baselines</div>',
    unsafe_allow_html=True,
)

try:
    with st.spinner("Connecting to the French grid data feed…"):
        data, source_status = load_data(settings.history_hours)
except (Eco2MixError, FileNotFoundError, ValueError) as exc:
    st.error(f"No official energy data is available yet: {exc}")
    st.code("python -m scripts.update_data --hours 72")
    st.stop()

latest = data.sort_values("timestamp").iloc[-1]
artifact, calibration_status = mood_artifact()
mood = classify_mood(latest.to_dict(), artifact)
local_time = latest["timestamp"].tz_convert(settings.timezone)
st.markdown(f'<span class="status">● {source_status}</span>', unsafe_allow_html=True)
st.caption(
    f"Latest observed interval: {local_time:%d %b %Y, %H:%M} Europe/Paris · "
    f"15-minute source cadence · mood thresholds: {calibration_status}"
)

cards = st.columns(4)
with cards[0]:
    metric_card("Current consumption", f"{latest['consumption_mw']:,.0f} MW")
with cards[1]:
    metric_card("Current CO₂ intensity", f"{latest['co2_intensity_g_per_kwh']:,.0f} g/kWh")
with cards[2]:
    metric_card("Renewable share", f"{latest['renewable_share']:.1%}", "Measured domestic generation")
with cards[3]:
    metric_card("Grid mood", mood["mood"], mood["reason"])

st.subheader("Demand pulse")
st.caption("How much electricity France is consuming across the loaded near-live or cached period.")
st.plotly_chart(consumption_chart(data), width="stretch")

left, right = st.columns([1.7, 1])
with left:
    st.subheader("What powers France")
    st.plotly_chart(production_area_chart(data), width="stretch")
with right:
    st.subheader("Latest energy mix")
    st.plotly_chart(mix_donut(latest), width="stretch")

if settings.weather_features_path.exists():
    weather = pd.read_parquet(settings.weather_features_path)
    weather = weather.loc[weather["timestamp"].between(data["timestamp"].min(), data["timestamp"].max())]
    render_weather_context(weather)
    if not weather.empty:
        joined = join_energy_weather(data[["timestamp", "consumption_mw"]], weather)
        overlap = joined[["consumption_mw", "weather_temperature_c"]].dropna()
        if len(overlap) >= 4:
            st.caption(
                "Recent demand/temperature correlation over aligned observations: "
                f"{overlap.corr().iloc[0, 1]:+.2f}. This is descriptive, not causal."
            )
else:
    st.info("Weather context is not cached yet. Run `python -m scripts.fetch_weather --start … --end …`.")

render_mood_explanation(mood, artifact)
with st.expander("Data quality and freshness", expanded=False):
    render_data_quality(data)

st.info(
    "Demand forecasts on the Baselines page are persistence and seasonal-naive references, "
    "not an AI model and not a claim of production forecasting quality."
)
