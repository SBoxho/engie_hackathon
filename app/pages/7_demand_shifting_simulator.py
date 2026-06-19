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

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.components.cards import message_box, metric_card, section_header, status_badge_html, viz_note
from app.components.charts import dark_chart_layout
from app.components.energy_weather import build_energy_weather_timeline
from app.components.layout import apply_theme
from src.config import settings
from src.data_processing.clean_energy_mix import clean_energy_mix
from src.data_processing.features import add_time_features
from src.data_processing.storage import PartitionedParquetStore
from src.demo_mode import demo_ecowatt, demo_energy, demo_model_evaluation, demo_mood_artifact
from src.data_sources.ecowatt import load_cached_ecowatt
from src.data_sources.rte_eco2mix import load_cached_eco2mix
from src.models.load_shift_simulator import (
    PRESSURE_COLORS,
    PRESSURE_POINTS,
    ShiftAction,
    ShiftScore,
    build_demo_timeline,
    compute_shift_score,
    load_actions,
    load_assumption_config,
    row_for_local_hour,
)

st.set_page_config(page_title="Shift the Load", page_icon=":material/tune:", layout="wide")
apply_theme()


def _format_hour(hour: int) -> str:
    return f"{hour:02d}:00"


def _format_mw(value: Any) -> str:
    numeric = pd.to_numeric(value, errors="coerce")
    if pd.isna(numeric):
        return "Unavailable"
    return f"{float(numeric):,.0f} MW"


def _format_co2(value: Any) -> str:
    numeric = pd.to_numeric(value, errors="coerce")
    if pd.isna(numeric):
        return "Unavailable"
    return f"{float(numeric):,.0f} g/kWh"


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _mood_artifact() -> dict[str, Any] | None:
    if settings.is_demo_mode:
        return demo_mood_artifact() or None
    return _load_json(settings.mood_artifact_path) or None


def _model_payload() -> dict[str, Any] | None:
    if settings.is_demo_mode:
        return demo_model_evaluation() or None
    path = settings.processed_dir / "demand_model" / "evaluation.json"
    return _load_json(path) or None


@st.cache_data(ttl=900, show_spinner=False)
def load_local_energy() -> tuple[pd.DataFrame, str]:
    """Load local grid context without making a network request."""
    if settings.is_demo_mode:
        energy = demo_energy()
        if not energy.empty:
            return energy.sort_values("timestamp"), "Demo energy sample"
        return pd.DataFrame(), "Offline demo grid profile"

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=7)
    store = PartitionedParquetStore(settings.energy_store_dir)
    try:
        recent = store.read(start=start, end=end)
        if not recent.empty:
            return recent.sort_values("timestamp"), "Local processed eco2mix snapshot"
        stored = store.read()
        if not stored.empty:
            return stored.sort_values("timestamp").tail(7 * 24 * 4), "Local processed eco2mix snapshot (historical)"
    except (OSError, ValueError):
        pass

    try:
        raw = load_cached_eco2mix()
        clean = add_time_features(clean_energy_mix(raw), settings.timezone)
        return clean.sort_values("timestamp"), "Cached raw eco2mix snapshot"
    except (FileNotFoundError, OSError, ValueError):
        return pd.DataFrame(), "Offline demo grid profile"


@st.cache_data(ttl=900, show_spinner=False)
def load_local_ecowatt(start: pd.Timestamp, end: pd.Timestamp) -> tuple[pd.DataFrame, str]:
    if settings.is_demo_mode:
        return demo_ecowatt(start, end)
    try:
        ecowatt = load_cached_ecowatt(timezone_name=settings.timezone)
    except (FileNotFoundError, OSError, ValueError):
        return pd.DataFrame(), "EcoWatt unavailable offline"
    if ecowatt.empty:
        return ecowatt, "EcoWatt unavailable offline"
    frame = ecowatt.loc[ecowatt["timestamp"].between(start, end)].copy()
    return frame, "Cached EcoWatt snapshot" if not frame.empty else "EcoWatt unavailable for this window"


def build_grid_context() -> tuple[pd.DataFrame, str, str]:
    energy, energy_source = load_local_energy()
    if energy.empty:
        return build_demo_timeline(timezone=settings.timezone), energy_source, "Demo assumptions"

    latest_ts = pd.to_datetime(energy["timestamp"], utc=True).max()
    start = latest_ts.floor("h") - pd.Timedelta(hours=1)
    end = latest_ts.floor("h") + pd.Timedelta(hours=25)
    ecowatt, ecowatt_source = load_local_ecowatt(start, end)
    result = build_energy_weather_timeline(
        energy,
        latest_ts=latest_ts,
        model_payload=_model_payload(),
        mood_artifact=_mood_artifact(),
        ecowatt=ecowatt,
        timezone=settings.timezone,
    )
    return result.timeline, energy_source, ecowatt_source


def comparison_card(title: str, row: pd.Series, *, role: str) -> None:
    status = str(row.get("status", "Unknown"))
    ecowatt = str(row.get("ecowatt_label", "Unavailable"))
    st.markdown(
        f"""
        <div class="ep-horizon-card ep-border-{_status_class(status)}">
          <div class="ep-card-row">
            <div class="ep-label">{html.escape(role)}</div>
            {status_badge_html(status, status)}
          </div>
          <div class="ep-value">{html.escape(title)}</div>
          <div class="ep-detail">
            Demand pressure: {html.escape(_format_mw(row.get("demand_signal_mw")))}<br>
            CO2 intensity: {html.escape(_format_co2(row.get("co2_intensity_g_per_kwh")))}<br>
            EcoWatt: {html.escape(ecowatt)}
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _status_class(status: str) -> str:
    lookup = {
        "Comfortable": "green",
        "Low-carbon opportunity": "blue",
        "Watch": "yellow",
        "Tense": "red",
        "Unknown": "grey",
    }
    return lookup.get(status, "grey")


def score_card(score: ShiftScore) -> None:
    st.markdown(
        f"""
        <div class="ep-explanation-card">
          <div class="ep-card-row">
            <div class="ep-label">Shift score</div>
            {status_badge_html("Educational", "blue")}
          </div>
          <div class="ep-value">{score.total_points:,} points</div>
          <div class="ep-detail">
            Grid relief: {score.grid_relief_points:,} points<br>
            Low-carbon bonus: {score.low_carbon_bonus:,} points<br>
            Peak-avoidance bonus: {score.peak_avoidance_bonus:,} points
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def timeline_chart(timeline: pd.DataFrame, original_hour: int, shifted_hour: int) -> go.Figure:
    frame = timeline.copy()
    frame["target"] = pd.to_datetime(frame["target"], utc=True, errors="coerce")
    frame = frame.dropna(subset=["target"]).sort_values("target")
    frame["local_hour"] = frame["target"].dt.tz_convert(settings.timezone).dt.hour
    if "hour_label" not in frame:
        frame["hour_label"] = frame["target"].dt.tz_convert(settings.timezone).dt.strftime("%H:%M")
    frame["pressure_points"] = frame["status"].map(PRESSURE_POINTS).fillna(0)
    frame["color"] = frame["status"].map(PRESSURE_COLORS).fillna(PRESSURE_COLORS["Unknown"])
    frame["selected"] = ""
    frame.loc[frame["local_hour"].eq(original_hour), "selected"] = "Before"
    frame.loc[frame["local_hour"].eq(shifted_hour), "selected"] = "After"

    fig = go.Figure()
    fig.add_bar(
        x=frame["hour_label"],
        y=frame["pressure_points"],
        marker_color=frame["color"],
        customdata=frame[["status", "demand_signal_mw", "co2_intensity_g_per_kwh", "selected"]],
        hovertemplate=(
            "<b>%{x}</b><br>Status: %{customdata[0]}<br>"
            "Demand: %{customdata[1]:,.0f} MW<br>CO2: %{customdata[2]:,.0f} g/kWh"
            "<br>%{customdata[3]}<extra></extra>"
        ),
    )
    selected = frame.loc[frame["local_hour"].isin([original_hour, shifted_hour])]
    fig.add_scatter(
        x=selected["hour_label"],
        y=selected["pressure_points"] + 0.28,
        mode="markers+text",
        marker=dict(size=15, color="#f8fafc", line=dict(color="#0f766e", width=2)),
        text=selected["selected"],
        textposition="top center",
        hoverinfo="skip",
    )
    fig.update_layout(
        **dark_chart_layout(
            height=280,
            margin=dict(l=10, r=10, t=12, b=10),
            xaxis_title=None,
            yaxis=dict(
                title=None,
                tickmode="array",
                tickvals=[0, 1, 2, 3],
                ticktext=["?", "OK", "Watch", "Tense"],
                range=[0, 3.7],
            ),
            showlegend=False,
        )
    )
    return fig


def render_action_assumption(action: ShiftAction) -> None:
    label = "Placeholder" if action.placeholder else "Fallback"
    status = "yellow" if action.placeholder else "blue"
    st.markdown(
        f"""
        <div class="ep-driver-card">
          <div class="ep-icon">{html.escape(action.icon)}</div>
          <div class="ep-card-row">
            <div class="ep-label">{html.escape(label)}</div>
            {status_badge_html(action.source_label, status)}
          </div>
          <div class="ep-title">{html.escape(action.label)}</div>
          <div class="ep-detail">
            {action.energy_kwh_per_event:.1f} kWh per household event over about {action.duration_hours} h.<br>
            {html.escape(action.source_detail)}
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


actions = load_actions()
assumption_config = load_assumption_config()
timeline, grid_source, ecowatt_source = build_grid_context()

section_header(
    "Shift the Load",
    "Move flexible use to an easier hour",
    "An educational simulator for seeing how timing, demand pressure, CO2 intensity, and EcoWatt context can change the energy-weather score.",
)
st.caption(settings.app_mode_label)
message_box(
    "Educational simulator",
    "This is a playful approximation, not an exact real-world savings calculator. Appliance values are transparent demo assumptions unless replaced by ADEME ElecDom data.",
    kind="info",
)

control, explainer = st.columns([1.05, 1])
with control:
    st.subheader("Choose your move")
    action_labels = {action.label: action_id for action_id, action in actions.items()}
    selected_label = st.selectbox(
        "Appliance or action",
        list(action_labels),
        index=list(action_labels).index("Dishwasher") if "Dishwasher" in action_labels else 0,
    )
    selected_action = actions[action_labels[selected_label]]
    households = int(
        st.number_input(
            "Households participating",
            min_value=1,
            max_value=500_000,
            value=1_000,
            step=100,
        )
    )
    original_hour = st.slider("Original hour", min_value=0, max_value=23, value=19, format="%02d:00")
    shifted_hour = st.slider("Shifted hour", min_value=0, max_value=23, value=3, format="%02d:00")

with explainer:
    render_action_assumption(selected_action)
    st.caption(f"Grid context: {grid_source}. EcoWatt context: {ecowatt_source}.")

original_row = row_for_local_hour(timeline, original_hour, timezone=settings.timezone)
shifted_row = row_for_local_hour(timeline, shifted_hour, timezone=settings.timezone)
score = compute_shift_score(selected_action, households, original_row, shifted_row)

section_header("Result", "Before and after")
metric_cols = st.columns(4)
with metric_cols[0]:
    metric_card("Approx shifted energy", f"{score.energy_mwh:.2f} MWh", "Event energy across participating households.", icon="MWh")
with metric_cols[1]:
    metric_card("Grid relief points", f"{score.grid_relief_points:,}", "More points when a move leaves a high-pressure hour.", icon="Grid")
with metric_cols[2]:
    metric_card("Low-carbon bonus", f"{score.low_carbon_bonus:,}", "Based on lower CO2 intensity at the shifted hour.", icon="CO2")
with metric_cols[3]:
    metric_card("Peak bonus", f"{score.peak_avoidance_bonus:,}", "Unlocked when the original hour is tense or watch-level.", icon="Peak")

before, after, total = st.columns([1, 1, 0.9])
with before:
    comparison_card(_format_hour(original_hour), original_row, role="Before")
with after:
    comparison_card(_format_hour(shifted_hour), shifted_row, role="After")
with total:
    score_card(score)

if score.original_pressure in {"Watch", "Tense"}:
    message = f"You helped avoid about {score.energy_mwh:.2f} MWh during a {score.original_pressure.lower()} hour."
else:
    message = f"You moved about {score.energy_mwh:.2f} MWh to compare a different hour."
if score.co2_delta_kg > 0:
    message = f"{message} The timing also avoids roughly {score.co2_delta_kg:.0f} kg CO2 in this simplified model."
elif score.co2_delta_kg < 0:
    message = f"{message} The shifted hour is not lower-carbon in this simplified model, so no low-carbon bonus is added."
message_box("Nice shift", message, kind="info")

section_header(
    "Mini timeline",
    "24-hour pressure map",
    "Bars show the educational pressure score for each hour. Before and after markers show your move.",
)
viz_note(
    "Before and after pressure",
    "The bar labels and markers show whether the selected appliance moves away from a watch or tense hour and toward a lower-pressure window.",
    source="Simulator context",
)
st.plotly_chart(timeline_chart(timeline, original_hour, shifted_hour), width="stretch")

with st.expander("Transparent assumptions and scoring", expanded=False):
    st.write(assumption_config["disclaimer"])
    for note in assumption_config.get("source_notes", []):
        st.caption(note)
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "action": action.label,
                    "kWh per household event": action.energy_kwh_per_event,
                    "duration hours": action.duration_hours,
                    "source": action.source_label,
                    "placeholder": action.placeholder,
                    "detail": action.source_detail,
                }
                for action in actions.values()
            ]
        ),
        width="stretch",
        hide_index=True,
    )
    st.write(
        "Scoring combines approximate shifted MWh, whether the move leaves a watch/tense hour, "
        "and whether the shifted hour has lower CO2 intensity. Points are game mechanics for learning, "
        "not a settlement, billing, or verified carbon accounting method."
    )

st.link_button("Back to public dashboard", "/")
