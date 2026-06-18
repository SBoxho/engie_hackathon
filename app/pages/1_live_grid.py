from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import streamlit as st

from app.components.cards import explanation_card, metric_card, section_header, status_badge, viz_note
from app.components.charts import MIX_COLUMNS
from app.components.layout import apply_theme
from app.components.regional_map import regional_comparison_bars, regional_demand_choropleth
from src.config import settings
from src.demo_mode import external_api_enabled, mode_badge_color
from src.data_sources.rte_eco2mix_regional import (
    RegionalEco2MixError,
    fallback_department_geojson,
    demo_regional_snapshot,
    fallback_region_geojson,
    fetch_regional_eco2mix,
    load_cached_regional_eco2mix,
    load_region_geojson,
    prepare_regional_snapshot,
    source_attribution,
)

apply_theme()


@st.cache_data(ttl=900, show_spinner=False)
def load_regional_data(hours: int) -> tuple[pd.DataFrame, str, bool]:
    if settings.is_demo_mode and not external_api_enabled():
        return demo_regional_snapshot(), "Demo regional snapshot, offline fallback", True
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    try:
        raw = fetch_regional_eco2mix(start=start, end=end)
        return prepare_regional_snapshot(raw), "Official regional RTE eco2mix, refreshed from ODRE", False
    except (RegionalEco2MixError, OSError):
        try:
            raw = load_cached_regional_eco2mix()
            return prepare_regional_snapshot(raw), "Official regional RTE eco2mix, cached snapshot", False
        except (RegionalEco2MixError, FileNotFoundError, ValueError, OSError):
            return demo_regional_snapshot(), "Demo regional snapshot, offline fallback", True


@st.cache_data(ttl=86400, show_spinner=False)
def load_regions() -> tuple[dict, str, bool]:
    if settings.is_demo_mode and not external_api_enabled():
        return fallback_region_geojson(), "Bundled simplified France region boundaries", True
    try:
        return load_region_geojson(), "Official French administrative regions via API Geo", False
    except (RegionalEco2MixError, AttributeError, TypeError, ValueError):
        return fallback_region_geojson(), "Bundled simplified France region boundaries", True


@st.cache_data(ttl=86400, show_spinner=False)
def load_departments() -> dict | None:
    try:
        return fallback_department_geojson()
    except (RegionalEco2MixError, OSError, ValueError):
        return None


def format_mw(value: float) -> str:
    return f"{value:,.0f} MW"


def selected_location(event: object) -> str | None:
    if event is None:
        return None
    selection = getattr(event, "selection", None)
    points = getattr(selection, "points", None) if selection is not None else None
    if points is None and isinstance(event, dict):
        points = event.get("selection", {}).get("points", [])
    if not points:
        return None
    point = points[0]
    if isinstance(point, dict):
        value = point.get("location")
        if value:
            return str(value)
        customdata = point.get("customdata") or []
        if customdata:
            return str(customdata[0])
    return None


def main_source(row: pd.Series) -> tuple[str, float]:
    values = {
        label: float(row.get(column, 0) or 0)
        for column, label in MIX_COLUMNS.items()
    }
    label = max(values, key=values.get)
    return label, values[label]


def mix_sentence(row: pd.Series) -> str:
    lead, value = main_source(row)
    renewable = float(row.get("renewable_share", 0) or 0)
    fossil = float(row.get("fossil_share", 0) or 0)
    return (
        f"{lead} is the largest measured source at {format_mw(value)}. "
        f"Renewables are {renewable:.0%} of local measured production; fossil output is {fossil:.0%}."
    )


def interpretation(row: pd.Series, frame: pd.DataFrame) -> tuple[str, str]:
    pressure = float(row["demand_pressure"])
    renewable = float(row.get("renewable_share", 0) or 0)
    co2 = float(row.get("co2_intensity_g_per_kwh", 0) or 0)
    median = float(frame["demand_pressure"].median())
    if pressure >= 0.82:
        title = "High regional demand pressure"
        detail = "This region is near the top of the current regional demand range."
    elif pressure >= median:
        title = "Demand is above the middle of the map"
        detail = "The region is not the national peak, but it is carrying visible load."
    else:
        title = "Demand pressure is lighter"
        detail = "The region sits in the lower half of the current regional demand range."
    if renewable >= 0.45 and co2 <= 45:
        detail = f"{detail} Renewable output is strong and the CO2 signal is relatively low."
    elif co2 >= 70:
        detail = f"{detail} The CO2 signal is worth watching in this snapshot."
    return title, detail


st.markdown('<div class="ep-eyebrow">Regional evidence layer</div>', unsafe_allow_html=True)
st.markdown('<div class="ep-hero">Regional pressure reveal</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="ep-subtitle">A France-first supporting view: where national demand, renewable availability, and local production pressure are concentrated right now.</div>',
    unsafe_allow_html=True,
)

regional, data_status, demo_data = load_regional_data(settings.history_hours)
regions_geojson, geo_status, demo_geo = load_regions()
department_geojson = load_departments()
status_badge(settings.app_mode_label, mode_badge_color())
status_badge(data_status, "grey" if demo_data else "blue")
status_badge(geo_status, "grey" if demo_geo else "blue")

latest_ts = regional["timestamp"].max()
local_ts = latest_ts.tz_convert(settings.timezone) if latest_ts.tzinfo else latest_ts
peak_region = regional.loc[regional["consumption_mw"].idxmax()]
renewable_region = regional.loc[regional["renewable_share"].idxmax()]
import_region = regional.loc[regional["regional_balance_mw"].idxmin()]
covered_regions = len(regional)
missing_regions = max(0, len(regions_geojson.get("features", [])) - covered_regions)

section_header(
    "Supporting evidence",
    "Regional demand pressure",
    "Color shows each region's demand relative to the current regional peak, so the national story can be grounded in where load and mix pressure are actually visible.",
)

explanation_card(
    "What this adds to the national story",
    "The forecast page explains the national direction; this regional layer shows whether pressure is concentrated in a few load centres, softened by renewable-rich regions, or exposed by local production shortfalls. It is evidence, not a separate forecast pipeline.",
    label="France-first context",
    status="info",
)

left, right = st.columns([1.65, 1], gap="large")
with left:
    viz_note(
        "Regional pressure map",
        "Hover or click a region to connect national pressure to three concrete signals: local demand, renewable share, and production balance.",
        source="RTE / ODRE + data.gouv.fr",
    )
    event = st.plotly_chart(
        regional_demand_choropleth(regional, regions_geojson, department_geojson),
        key="regional_demand_map",
        width="stretch",
        on_select="rerun",
        selection_mode="points",
    )
    event_code = selected_location(event)
    if event_code in set(regional["region_code"]):
        st.session_state["selected_region_code"] = event_code

with right:
    codes = regional["region_code"].tolist()
    stored_code = st.session_state.get("selected_region_code")
    if stored_code not in codes:
        stored_code = str(peak_region["region_code"])
        st.session_state["selected_region_code"] = stored_code
    labels = regional.set_index("region_code")["region_display"].to_dict()
    selected_code = st.selectbox(
        "Selected region",
        codes,
        index=codes.index(stored_code),
        format_func=lambda code: labels.get(code, code),
    )
    st.session_state["selected_region_code"] = selected_code
    selected = regional.loc[regional["region_code"] == selected_code].iloc[0]
    title, detail = interpretation(selected, regional)

    metric_card(
        "Demand",
        format_mw(float(selected["consumption_mw"])),
        f"Rank #{int(selected['demand_rank'])}; {float(selected['national_demand_share']):.1%} of covered regional demand.",
        icon="kW",
        status=str(selected["pressure_band"]),
    )
    metric_card(
        "Renewable share",
        f"{float(selected['renewable_share']):.0%}",
        f"Rank #{int(selected['renewable_rank'])}; {mix_sentence(selected)}",
        icon="RES",
    )
    balance = float(selected["regional_balance_mw"])
    metric_card(
        "Local balance",
        f"{balance:+,.0f} MW",
        "Positive means measured regional production exceeds local demand; negative means the region leans on the wider grid.",
        icon="Grid",
        status="watch" if balance < 0 else "good",
    )
    explanation_card(
        title,
        detail,
        label=str(selected["region_display"]),
        status="watch" if float(selected["demand_pressure"]) >= 0.82 else "info",
    )

section_header("Compare", "Fast regional readout", "The top demand regions are highlighted for quick comparison; the selected region is shown in yellow.")
st.plotly_chart(regional_comparison_bars(regional, st.session_state.get("selected_region_code")), width="stretch")

section_header("Snapshot", "Regional highlights")
cols = st.columns(4)
with cols[0]:
    metric_card("Last update", f"{local_ts:%H:%M}", f"{local_ts:%d %b %Y}, Europe/Paris.", icon="Now")
with cols[1]:
    metric_card(
        "Peak demand",
        str(peak_region["region_display"]),
        format_mw(float(peak_region["consumption_mw"])),
        icon="Peak",
    )
with cols[2]:
    metric_card(
        "Renewable leader",
        str(renewable_region["region_display"]),
        f"{float(renewable_region['renewable_share']):.0%} renewable share.",
        icon="RES",
    )
with cols[3]:
    metric_card(
        "Grid dependency",
        str(import_region["region_display"]),
        f"Largest local shortfall: {float(import_region['regional_balance_mw']):+,.0f} MW.",
        icon="Grid",
        status="watch",
    )

if missing_regions:
    st.warning(
        f"Regional data is partially available for this snapshot: {covered_regions} regions are covered and {missing_regions} map regions have no live row. Demo stability is preserved by keeping the available rows and labelled fallbacks."
    )

with st.expander("Regional values", expanded=False):
    display = regional[
        [
            "region_display",
            "consumption_mw",
            "total_production_mw",
            "renewable_share",
            "co2_intensity_g_per_kwh",
            "demand_pressure",
            "national_demand_share",
            "regional_balance_mw",
            "pressure_band",
        ]
    ].rename(
        columns={
            "region_display": "Region",
            "consumption_mw": "Demand MW",
            "total_production_mw": "Production MW",
            "renewable_share": "Renewable share",
            "co2_intensity_g_per_kwh": "CO2 g/kWh",
            "demand_pressure": "Demand pressure",
            "national_demand_share": "Covered demand share",
            "regional_balance_mw": "Local balance MW",
            "pressure_band": "Pressure band",
        }
    )
    st.dataframe(display, width="stretch", hide_index=True)

sources = source_attribution()
st.caption(
    "Sources: regional RTE eco2mix via "
    f"[ODRE]({sources['regional_eco2mix']}); French administrative region geometry via "
    f"[data.gouv.fr/API Geo]({sources['regional_geojson']}). "
    "Offline fallback boundaries use simplified IGN/INSEE-derived GeoJSON from "
    f"[france-geojson]({sources['regional_geojson_fallback']}); department outlines from "
    f"[france-geojson]({sources['department_geojson_fallback']}). "
    "If live regional electricity data is unavailable, this page uses a labelled static demo data snapshot."
)
