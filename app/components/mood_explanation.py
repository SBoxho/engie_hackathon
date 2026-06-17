"""Streamlit explanation for the educational mood calibration."""
from __future__ import annotations

from typing import Any, Mapping


def render_mood_explanation(result: Mapping[str, Any], artifact: Mapping[str, Any], *, ui=None) -> None:
    """Render decision details without presenting the mood as an RTE alert."""
    if ui is None:
        import streamlit as ui

    ui.subheader(f"Grid mood: {result['mood']}")
    ui.write(result["reason"])
    ui.caption(
        "Educational indicator only — this is not an RTE operational alert, "
        "grid-security assessment, or recommendation to change consumption."
    )
    segment = result["segment"]
    fallback = result["fallback"]
    ui.write(
        f"Reference: {segment['season']} at local hour {segment['local_hour']:02d}:00 "
        f"(Europe/Paris); calibration level: {segment['level']}; "
        f"sample: {result['sample']}; fallback: {fallback}."
    )
    with ui.expander("How this mood was calibrated"):
        ui.json({
            "thresholds": result["thresholds"],
            "precedence": artifact.get("precedence"),
            "source": artifact.get("source"),
            "generated_at": artifact.get("generated_at"),
            "quantile_method": artifact.get("quantile_method"),
        })

