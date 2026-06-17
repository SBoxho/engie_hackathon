"""Reusable Streamlit panel for national weather context."""
from __future__ import annotations

import pandas as pd


def render_weather_context(features: pd.DataFrame) -> None:
    import streamlit as st

    st.subheader("Population-weighted weather context")
    if features.empty:
        st.info("No weather context is available for this interval.")
        return
    latest = features.sort_values("timestamp").iloc[-1]
    cols = st.columns(4)
    cols[0].metric("Temperature", f"{latest['weather_temperature_c']:.1f} °C")
    cols[1].metric("Wind", f"{latest['weather_wind_speed_kmh']:.1f} km/h")
    cols[2].metric("Humidity", f"{latest['weather_humidity_pct']:.0f}%")
    cols[3].metric("City coverage", f"{latest['weather_population_coverage']:.0%}")
    chart = features.set_index("timestamp")[[
        "weather_temperature_c", "weather_wind_speed_kmh", "weather_cloud_cover_pct"
    ]]
    st.line_chart(chart)
    if latest["weather_population_coverage"] < 1:
        missing = latest.get("weather_missing_cities") or "unknown"
        st.warning(f"Partial population coverage; missing cities: {missing}")
    st.caption(
        "Open-Meteo hourly observations, aligned backward to UTC quarter-hours; "
        "weights use INSEE 2022 municipal populations."
    )
