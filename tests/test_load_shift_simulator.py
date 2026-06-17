from __future__ import annotations

import pandas as pd

from src.models.load_shift_simulator import (
    build_demo_timeline,
    compute_shift_score,
    load_actions,
    row_for_local_hour,
)


def test_load_shift_assumptions_are_explicit_and_positive():
    actions = load_actions()

    assert {"washing_machine", "dishwasher", "oven", "ev_charging", "heating_reduction"}.issubset(actions)
    assert actions["washing_machine"].energy_kwh_per_event > 0
    assert actions["ev_charging"].placeholder
    assert actions["heating_reduction"].source_label == "Placeholder assumption"


def test_demo_timeline_is_offline_24h_context():
    timeline = build_demo_timeline(start=pd.Timestamp("2026-06-17T00:00:00Z"))

    assert len(timeline) == 24
    assert timeline["demand_signal_mw"].notna().all()
    assert timeline["co2_intensity_g_per_kwh"].notna().all()
    assert "Tense" in set(timeline["status"])
    assert set(timeline["ecowatt_status"]) == {"unknown"}


def test_shift_score_rewards_peak_and_lower_carbon_move():
    actions = load_actions()
    timeline = build_demo_timeline(start=pd.Timestamp("2026-06-17T00:00:00Z"))
    original = row_for_local_hour(timeline, 19, timezone="Europe/Paris")
    shifted = row_for_local_hour(timeline, 3, timezone="Europe/Paris")

    score = compute_shift_score(actions["dishwasher"], 1000, original, shifted)

    assert score.energy_mwh == 1.0
    assert score.original_pressure == "Tense"
    assert score.shifted_pressure in {"Comfortable", "Low-carbon opportunity"}
    assert score.peak_avoidance_bonus > 0
    assert score.total_points > score.grid_relief_points
