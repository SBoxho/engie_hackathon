from __future__ import annotations

import html
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

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

st.set_page_config(page_title="Energy Pulse France", page_icon=":zap:", layout="wide")
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
        return clean, "Official RTE eco2mix, refreshed from ODRE"
    except (Eco2MixError, OSError):
        stored = PartitionedParquetStore(settings.energy_store_dir).read(start=start, end=end)
        if not stored.empty:
            return stored, "Official RTE eco2mix, local partitioned snapshot"
        raw = load_cached_eco2mix()
        return clean_energy_mix(raw), "Official RTE eco2mix, cached raw snapshot"


@st.cache_data(ttl=900, show_spinner=False)
def load_weather(start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    if not settings.weather_features_path.exists():
        return pd.DataFrame()
    weather = pd.read_parquet(settings.weather_features_path)
    return weather.loc[weather["timestamp"].between(start, end)].copy()


@st.cache_data(ttl=900, show_spinner=False)
def load_model_evaluation() -> dict[str, Any]:
    path = settings.processed_dir / "demand_model" / "evaluation.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def mood_artifact() -> tuple[dict[str, Any], str]:
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


def card(icon: str, label: str, value: str, detail: str) -> None:
    st.markdown(
        f"""
        <div class="pulse-card">
          <div class="pulse-icon">{icon}</div>
          <div class="pulse-label">{html.escape(label)}</div>
          <div class="pulse-value">{html.escape(value)}</div>
          <div class="pulse-detail">{html.escape(detail)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def driver_card(icon: str, title: str, detail: str) -> None:
    st.markdown(
        f"""
        <div class="driver-card">
          <div class="driver-icon">{icon}</div>
          <div class="driver-label">Driver</div>
          <div class="driver-title">{html.escape(title)}</div>
          <div class="driver-detail">{html.escape(detail)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def section(kicker: str, title: str, copy: str | None = None) -> None:
    st.markdown(f'<div class="section-kicker">{html.escape(kicker)}</div>', unsafe_allow_html=True)
    st.markdown(f'<div class="section-title">{html.escape(title)}</div>', unsafe_allow_html=True)
    if copy:
        st.markdown(f'<div class="section-copy">{html.escape(copy)}</div>', unsafe_allow_html=True)


def format_mw(value: float) -> str:
    return f"{value:,.0f} MW"


def weather_summary(weather: pd.DataFrame) -> tuple[str, str, dict[str, float] | None]:
    if weather.empty:
        return "Not cached", "Weather context is available after running the weather pipeline.", None
    latest = weather.sort_values("timestamp").iloc[-1]
    temp = float(latest.get("weather_temperature_c", 0))
    wind = float(latest.get("weather_wind_speed_kmh", 0))
    cloud = float(latest.get("weather_cloud_cover_pct", 0))
    if temp <= 5:
        headline = "Cold lift"
        detail = f"Cold weather can lift heating demand. Latest national proxy: {temp:.1f} C."
    elif temp >= 27:
        headline = "Heat lift"
        detail = f"Hot weather can lift cooling demand. Latest national proxy: {temp:.1f} C."
    elif wind >= 35:
        headline = "Windy"
        detail = f"Wind is noticeable at {wind:.0f} km/h, with cloud cover near {cloud:.0f}%."
    else:
        headline = "Mild"
        detail = f"Weather pressure looks moderate: {temp:.1f} C, wind {wind:.0f} km/h."
    return headline, detail, {"temp": temp, "wind": wind, "cloud": cloud}


def demand_pressure(value: float, quantiles: pd.Series) -> tuple[str, str, float]:
    q25 = float(quantiles.loc[0.25])
    q60 = float(quantiles.loc[0.60])
    q85 = float(quantiles.loc[0.85])
    if value >= q85:
        return "High", "#fb7185", 1.0
    if value >= q60:
        return "Elevated", "#fbbf24", 0.72
    if value <= q25:
        return "Light", "#38bdf8", 0.28
    return "Normal", "#5eead4", 0.5


def build_pressure_forecast(data: pd.DataFrame, latest_ts: pd.Timestamp) -> tuple[pd.DataFrame, str]:
    frame = data[["timestamp", "consumption_mw"]].dropna().sort_values("timestamp").copy()
    quantiles = frame["consumption_mw"].quantile([0.25, 0.60, 0.85])
    start = latest_ts.floor("h") + pd.Timedelta(hours=1)
    targets = pd.DataFrame({"target": pd.date_range(start=start, periods=24, freq="h")})
    targets["reference_timestamp"] = targets["target"] - pd.Timedelta(hours=24)
    reference = frame.rename(columns={"timestamp": "reference_timestamp"})
    forecast = pd.merge_asof(
        targets.sort_values("reference_timestamp"),
        reference,
        on="reference_timestamp",
        direction="nearest",
        tolerance=pd.Timedelta(minutes=45),
    ).sort_values("target")
    source = "Yesterday's same-hour demand, translated into pressure bands."
    if forecast["consumption_mw"].isna().any():
        recent = frame.tail(24).reset_index(drop=True)
        forecast["consumption_mw"] = forecast["consumption_mw"].fillna(
            pd.Series(recent["consumption_mw"].tolist() * 2).iloc[: len(forecast)].to_numpy()
        )
        source = "Recent demand pattern, translated into pressure bands."
    pressure = forecast["consumption_mw"].apply(lambda value: demand_pressure(float(value), quantiles))
    forecast[["pressure", "color", "height"]] = pd.DataFrame(pressure.tolist(), index=forecast.index)
    return forecast, source


def pressure_timeline(forecast: pd.DataFrame) -> go.Figure:
    labels = forecast["target"].dt.tz_convert(settings.timezone).dt.strftime("%H:%M")
    fig = go.Figure(
        go.Bar(
            x=labels,
            y=forecast["height"],
            marker_color=forecast["color"],
            customdata=forecast[["pressure", "consumption_mw"]],
            hovertemplate="<b>%{x}</b><br>Pressure: %{customdata[0]}<br>Reference: %{customdata[1]:,.0f} MW<extra></extra>",
        )
    )
    fig.update_layout(
        height=260,
        margin=dict(l=10, r=10, t=10, b=10),
        xaxis_title=None,
        yaxis=dict(visible=False, range=[0, 1.08]),
        hovermode="x",
        showlegend=False,
    )
    return fig


def render_model_honesty(payload: dict[str, Any]) -> None:
    comparisons = pd.DataFrame(payload.get("baseline_comparison", []))
    if comparisons.empty:
        st.markdown(
            """
            <div class="honesty-card">
              <div class="pulse-label">Model honesty</div>
              <div class="pulse-value">No evaluation yet</div>
              <div class="honesty-detail">Run the demand-model evaluation to show which horizons beat simple baselines.</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        return

    comparisons = comparisons.sort_values("horizon_hours")
    wins = comparisons.loc[comparisons["improvement_vs_strongest_baseline_percent"] > 0]
    misses = comparisons.loc[comparisons["improvement_vs_strongest_baseline_percent"] <= 0]
    win_text = ", ".join(f"{int(row.horizon_hours)}h" for row in wins.itertuples()) or "none yet"
    miss_text = ", ".join(f"{int(row.horizon_hours)}h" for row in misses.itertuples()) or "none"
    generated = payload.get("generated_at", "unknown")
    st.markdown(
        f"""
        <div class="honesty-card">
          <div class="pulse-label">Model honesty</div>
          <div class="pulse-value">Beats baseline at: <span class="honesty-good">{html.escape(win_text)}</span></div>
          <div class="honesty-detail">Still weaker or tied at: <span class="honesty-watch">{html.escape(miss_text)}</span>. Artifact generated: {html.escape(str(generated))}.</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


try:
    with st.spinner("Connecting to the French grid data feed..."):
        data, source_status = load_data(settings.history_hours)
except (Eco2MixError, FileNotFoundError, ValueError) as exc:
    st.error(f"No official energy data is available yet: {exc}")
    st.code("python -m scripts.update_data --hours 72")
    st.stop()

data = data.sort_values("timestamp")
latest = data.iloc[-1]
artifact, calibration_status = mood_artifact()
mood = classify_mood(latest.to_dict(), artifact)
local_time = latest["timestamp"].tz_convert(settings.timezone)
weather = load_weather(data["timestamp"].min(), data["timestamp"].max())
weather_headline, weather_detail, weather_values = weather_summary(weather)
forecast, forecast_source = build_pressure_forecast(data, latest["timestamp"])

st.markdown('<div class="eyebrow">France electricity weather</div>', unsafe_allow_html=True)
st.markdown('<div class="hero">Energy Pulse France</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="subtitle">A public energy-weather app that turns official French grid data into a fast read on demand, carbon, weather pressure, and what comes next.</div>',
    unsafe_allow_html=True,
)
st.markdown(f'<span class="status">{html.escape(source_status)}</span>', unsafe_allow_html=True)

section("Live signal", "Current grid pulse")
cols = st.columns(5)
with cols[0]:
    card("&#9889;", "Demand", format_mw(float(latest["consumption_mw"])), "How much power France is using now.")
with cols[1]:
    card("CO2", "CO2 intensity", f"{float(latest['co2_intensity_g_per_kwh']):,.0f} g/kWh", "Carbon signal from the official source.")
with cols[2]:
    card("&#9679;", "Grid mood", str(mood["mood"]), str(mood["reason"]))
with cols[3]:
    card("&#9729;", "Weather influence", weather_headline, weather_detail)
with cols[4]:
    card("&#8635;", "Last update", f"{local_time:%H:%M}", f"{local_time:%d %b %Y}, Europe/Paris.")

section(
    "Next 24h",
    "Demand pressure forecast",
    "A simple visual outlook for when the grid may feel light, normal, elevated, or high pressure.",
)
st.plotly_chart(pressure_timeline(forecast), width="stretch")
st.caption(f"{forecast_source} This is a demo outlook, not an RTE operational forecast.")

section("Why", "What is moving the pulse?")
history_quantiles = data["consumption_mw"].quantile([0.25, 0.60, 0.85])
current_pressure, _, _ = demand_pressure(float(latest["consumption_mw"]), history_quantiles)
renewable_share = float(latest.get("renewable_share", 0))
co2_intensity = float(latest.get("co2_intensity_g_per_kwh", 0))
driver_cols = st.columns(4)
with driver_cols[0]:
    driver_card(
        "&#128200;",
        f"Demand is {current_pressure.lower()}",
        f"Current use is {format_mw(float(latest['consumption_mw']))}, compared with the recent range.",
    )
with driver_cols[1]:
    driver_card("&#127777;", weather_headline, weather_detail)
with driver_cols[2]:
    driver_card(
        "&#9728;",
        "Clean supply is visible",
        f"Renewables are contributing {renewable_share:.1%} of measured domestic generation.",
    )
with driver_cols[3]:
    driver_card(
        "CO2",
        "Carbon signal stays explicit",
        f"The latest source intensity is {co2_intensity:,.0f} g/kWh, shown separately from demand.",
    )

section("Action", "What can I do?")
st.markdown(
    """
    <div class="action-panel">
      <div class="action-title">Try shifting flexible demand away from high-pressure hours.</div>
      <div class="pulse-detail">The simulator placeholder is ready for the hackathon story: choose an appliance, move it on the timeline, and show how the idea would reduce pressure once the model is connected.</div>
    </div>
    """,
    unsafe_allow_html=True,
)
st.page_link("pages/7_demand_shifting_simulator.py", label="Open demand-shifting simulator", icon=":material/tune:")

section("Honesty", "Model honesty")
render_model_honesty(load_model_evaluation())

with st.expander("Advanced / Data Science", expanded=False):
    st.write("Deep-dive pages for the technical jury and for continuing development.")
    link_cols = st.columns(3)
    with link_cols[0]:
        st.page_link("pages/1_live_grid.py", label="Live grid detail")
        st.page_link("pages/2_forecast.py", label="Forecast workspace")
    with link_cols[1]:
        st.page_link("pages/3_explainability.py", label="Explainability")
        st.page_link("pages/4_historical.py", label="Historical grid")
    with link_cols[2]:
        st.page_link("pages/5_baselines.py", label="Demand baselines")
        st.page_link("pages/6_demand_model.py", label="Demand model")

    st.divider()
    left, right = st.columns([1.35, 1])
    with left:
        st.subheader("Demand pulse detail")
        st.plotly_chart(consumption_chart(data), width="stretch")
        st.subheader("What powers France")
        st.plotly_chart(production_area_chart(data), width="stretch")
    with right:
        st.subheader("Latest energy mix")
        st.plotly_chart(mix_donut(latest), width="stretch")

    if weather.empty:
        st.info("Weather context is not cached yet. Run `python -m scripts.fetch_weather --start ... --end ...`.")
    else:
        render_weather_context(weather)
        joined = join_energy_weather(data[["timestamp", "consumption_mw"]], weather)
        overlap = joined[["consumption_mw", "weather_temperature_c"]].dropna()
        if len(overlap) >= 4:
            st.caption(
                "Recent demand/temperature correlation over aligned observations: "
                f"{overlap.corr().iloc[0, 1]:+.2f}. This is descriptive, not causal."
            )

    render_mood_explanation(mood, artifact)
    st.caption(f"Mood thresholds: {calibration_status}.")
    with st.expander("Data quality and freshness", expanded=False):
        render_data_quality(data)
