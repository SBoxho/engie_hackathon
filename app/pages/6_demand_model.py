"""Experimental demand model dashboard page."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EVALUATION = PROJECT_ROOT / "data" / "processed" / "demand_model" / "evaluation.json"
BASELINE_LABELS = {
    "persistence": "Persistence",
    "day_naive": "Previous day",
    "week_naive": "Previous week",
}
SEASON_LABELS = {0: "Winter", 1: "Spring", 2: "Summer", 3: "Autumn"}


@st.cache_data(show_spinner=False)
def load_evaluation(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


st.title("Demand model")
st.caption(
    "Experimental weather-aware demand model. This is not an RTE operational forecast "
    "and should only be read as a backtested research artifact."
)

artifact_path = st.text_input("Evaluation artifact", str(DEFAULT_EVALUATION))
try:
    payload = load_evaluation(artifact_path)
except (OSError, ValueError, json.JSONDecodeError) as exc:
    st.info("Run the demand model pipeline before opening this page.")
    st.code(
        "python -m scripts.build_features\n"
        "python -m scripts.train_demand_model\n"
        "python -m scripts.evaluate_demand_model"
    )
    st.caption(f"Artifact unavailable: {exc}")
    st.stop()

predictions = pd.DataFrame(payload.get("predictions", []))
metrics = pd.DataFrame(payload.get("metrics", []))
comparisons = pd.DataFrame(payload.get("baseline_comparison", []))
if predictions.empty or metrics.empty:
    st.warning("The evaluation artifact contains no model predictions.")
    st.stop()

for column in ("origin_timestamp", "target_timestamp"):
    predictions[column] = pd.to_datetime(predictions[column], utc=True)

data_audit = payload.get("data_audit", {})
weather_audit = data_audit.get("weather") or {}
latest_target = predictions["target_timestamp"].max()
st.write(
    f"Artifact generated: **{payload.get('generated_at', 'unknown')}** · "
    f"latest evaluated target: **{latest_target.strftime('%Y-%m-%d %H:%M UTC')}**"
)
st.write(
    f"Training source coverage: **{data_audit.get('start_utc', 'unknown')} → "
    f"{data_audit.get('end_utc', 'unknown')}** · "
    f"weather overlap: **{weather_audit.get('overlap_fraction_of_energy_timestamps', 0):.1%}**"
)

left, right = st.columns(2)
horizon = left.selectbox(
    "Forecast horizon",
    sorted(predictions["horizon_hours"].unique()),
    format_func=lambda value: f"{int(value)} hours",
)
available_comparison = comparisons.loc[comparisons["horizon_hours"].eq(horizon)]
if not available_comparison.empty:
    row = available_comparison.iloc[0]
    strongest = row.get("strongest_baseline") or "none"
    improvement = row.get("improvement_vs_strongest_baseline_percent")
    right.metric(
        "Improvement vs strongest baseline",
        "n/a" if pd.isna(improvement) else f"{improvement:+.1f}%",
        help=f"Strongest eligible baseline: {BASELINE_LABELS.get(strongest, strongest)}",
    )

selected = predictions.loc[predictions["horizon_hours"].eq(horizon)].copy()
selected = selected.sort_values("target_timestamp").tail(7 * 96)
baseline_column = None
if not available_comparison.empty and pd.notna(available_comparison.iloc[0].get("strongest_baseline")):
    baseline_column = f"{available_comparison.iloc[0]['strongest_baseline']}_predicted_mw"
if baseline_column not in selected:
    baseline_column = "persistence_predicted_mw"

chart = selected[["target_timestamp", "target_mw", "model_predicted_mw", baseline_column]].rename(
    columns={
        "target_mw": "Actual demand",
        "model_predicted_mw": "Model",
        baseline_column: BASELINE_LABELS.get(baseline_column.replace("_predicted_mw", ""), "Baseline"),
    }
)
chart = chart.melt("target_timestamp", var_name="series", value_name="MW")
figure = px.line(chart, x="target_timestamp", y="MW", color="series")
figure.update_layout(xaxis_title=None, legend_title=None, hovermode="x unified")
st.plotly_chart(figure, width="stretch")

metric_view = metrics.loc[metrics["horizon_hours"].eq(horizon)].copy()
metric_view["model"] = metric_view["model"].map(BASELINE_LABELS).fillna(metric_view["model"])
st.dataframe(metric_view, width="stretch", hide_index=True)

periods = payload.get("training_periods", {}).get(str(int(horizon)), {})
with st.expander("Training and evaluation period"):
    st.json(periods)

segments = pd.DataFrame(payload.get("segment_metrics", []))
with st.expander("Segment performance"):
    if segments.empty:
        st.caption("No hour or season segment has enough samples for a stable summary.")
    else:
        view = segments.loc[segments["horizon_hours"].eq(horizon)].copy()
        if "segment_value" in view:
            view["segment_value"] = view["segment_value"].astype(str)
            is_season = view["segment"].eq("target_season")
            season_values = pd.to_numeric(view.loc[is_season, "segment_value"], errors="coerce")
            view.loc[is_season, "segment_value"] = season_values.map(SEASON_LABELS).fillna(
                view.loc[is_season, "segment_value"]
            )
        st.dataframe(view, width="stretch", hide_index=True)

with st.expander("Data audit"):
    st.json(data_audit)
