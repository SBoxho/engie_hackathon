from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pandas as pd
import pytest

from app.components.mood_explanation import render_mood_explanation
from scripts.calibrate_mood import read_calibration_source
from src.data_processing.storage import PartitionedParquetStore
from src.models.mood_calibration import add_local_segment, calibrate_mood, classify_mood


def frame_at(timestamps, *, consumption=None) -> pd.DataFrame:
    size = len(timestamps)
    return pd.DataFrame({
        "timestamp": timestamps,
        "region": ["France"] * size,
        "consumption_mw": consumption or list(range(1, size + 1)),
        "co2_intensity_g_per_kwh": list(range(10, 10 + size)),
        "renewable_share": [0.2 + index / 100 for index in range(size)],
        "fossil_share": [0.05 + index / 100 for index in range(size)],
    })


def test_local_segments_handle_both_dst_transitions():
    segmented = add_local_segment(frame_at([
        "2024-03-31T00:30:00Z", "2024-03-31T01:30:00Z",
        "2024-10-27T00:30:00Z", "2024-10-27T01:30:00Z",
    ]))
    assert segmented["local_hour"].tolist() == [1, 3, 2, 2]
    assert segmented["season"].tolist() == ["spring", "spring", "autumn", "autumn"]


def test_quantiles_are_linear_and_artifact_metadata_is_deterministic():
    data = frame_at(pd.date_range("2024-06-01", periods=4, freq="h", tz="UTC"))
    artifact = calibrate_mood(data, min_sample=1, generated_at="2025-01-01T00:00:00Z")
    global_segment = next(item for item in artifact["segments"] if item["level"] == "global")
    assert global_segment["thresholds"]["consumption_high"] == pytest.approx(3.55)
    assert artifact["source"]["period_start"] == "2024-06-01T00:00:00Z"
    assert artifact["generated_at"] == "2025-01-01T00:00:00Z"
    assert artifact == calibrate_mood(data, min_sample=1, generated_at="2025-01-01T00:00:00Z")


def test_fallback_order_uses_season_before_hour_and_global():
    timestamps = pd.date_range("2024-06-01", periods=6, freq="h", tz="UTC")
    artifact = calibrate_mood(frame_at(timestamps), min_sample=4, generated_at="2025-01-01T00:00:00Z")
    result = classify_mood(frame_at([timestamps[0]]).iloc[0].to_dict(), artifact)
    assert result["segment"]["level"] == "season"
    assert result["fallback"] == "season"
    assert result["sample"] == 6


def test_fallback_reaches_fixed_thresholds_only_after_all_calibrations_fail():
    artifact = {
        "timezone": "Europe/Paris", "min_sample": 10, "segments": [],
        "fixed_thresholds": {
            "consumption_high": 70000, "co2_low": 40, "co2_high": 80,
            "renewable_high": 0.35, "fossil_high": 0.15,
        },
    }
    observation = frame_at(["2024-01-01T00:00:00Z"], consumption=[50_000]).iloc[0].to_dict()
    result = classify_mood(observation, artifact)
    assert result["segment"]["level"] == "fixed"
    assert result["fallback"] == "fixed"
    assert result["sample"] == 0


def test_precedence_is_carbon_then_tense_then_renewable_then_calm():
    artifact = calibrate_mood(
        frame_at(pd.date_range("2024-06-01", periods=8, freq="h", tz="UTC")),
        min_sample=1,
        generated_at="2025-01-01T00:00:00Z",
    )
    base = frame_at(["2024-06-01T00:00:00Z"]).iloc[0].to_dict()
    base.update(consumption_mw=1_000_000, co2_intensity_g_per_kwh=1_000,
                renewable_share=1.0, fossil_share=1.0)
    assert classify_mood(base, artifact)["mood"] == "Carbon-heavy"
    base.update(co2_intensity_g_per_kwh=0, fossil_share=0)
    assert classify_mood(base, artifact)["mood"] == "Tense"


def test_cli_reads_agent2_partition_contract(tmp_path):
    root = tmp_path / "partitions"
    expected = frame_at(["2024-06-01T00:00:00Z"])
    PartitionedParquetStore(root).upsert(expected)
    loaded, source = read_calibration_source(None, root)
    assert len(loaded) == 1
    assert source.startswith("partitioned-store:")


def test_documented_cli_command_writes_artifact(tmp_path):
    input_path = tmp_path / "input.parquet"
    output_path = tmp_path / "artifact.json"
    frame_at(pd.date_range("2024-06-01", periods=4, freq="h", tz="UTC")).to_parquet(input_path)
    completed = subprocess.run([
        sys.executable, "scripts/calibrate_mood.py", "--input", str(input_path),
        "--output", str(output_path), "--min-sample", "1",
        "--generated-at", "2025-01-01T00:00:00Z",
    ], capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr
    assert output_path.exists()
    assert "calibrated segments" in completed.stdout


class FakeUi:
    def __init__(self):
        self.messages = []
    def subheader(self, value): self.messages.append(value)
    def write(self, value): self.messages.append(value)
    def caption(self, value): self.messages.append(value)
    def json(self, value): self.messages.append(value)
    def expander(self, value):
        self.messages.append(value)
        return self
    def __enter__(self): return self
    def __exit__(self, *args): return None


def test_ui_says_indicator_is_educational_not_an_rte_alert():
    ui = FakeUi()
    result = {
        "mood": "Calm", "reason": "test", "thresholds": {}, "sample": 12,
        "fallback": "none", "segment": {"season": "summer", "local_hour": 14,
                                                "level": "season_hour"},
    }
    render_mood_explanation(result, {"precedence": []}, ui=ui)
    rendered = " ".join(str(value) for value in ui.messages)
    assert "Educational indicator only" in rendered
    assert "not an RTE operational alert" in rendered


def test_offline_smoke_with_cached_real_eco2mix_data():
    """Exercise checked-in/workspace official data without making a network call."""
    path = Path("data/processed/eco2mix_latest.parquet")
    if not path.exists():
        pytest.skip("official processed smoke fixture is not present")
    real = pd.read_parquet(path)
    artifact = calibrate_mood(real, min_sample=1, generated_at="2026-06-17T00:00:00Z")
    result = classify_mood(real.sort_values("timestamp").iloc[-1].to_dict(), artifact)
    assert result["mood"] in {"Calm", "Tense", "Carbon-heavy", "Renewable-rich"}
    assert artifact["source"]["rows"] == len(real.dropna(subset=[
        "consumption_mw", "co2_intensity_g_per_kwh", "renewable_share", "fossil_share"
    ]))
