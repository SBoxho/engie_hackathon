import pandas as pd
import pytest

from src.data_processing.clean_energy_mix import clean_energy_mix, grid_mood
from src.data_processing.features import add_time_features
from tests.test_data_fetching import record


def test_cleaning_derives_shares_and_export_sign():
    clean = clean_energy_mix(pd.DataFrame([record()]))
    row = clean.iloc[0]
    assert row["exports_mw"] == 6900
    assert row["imports_mw"] == 0
    assert 0 < row["renewable_share"] < 1
    assert row["total_production_mw"] == 56920


def test_time_features_are_created():
    clean = clean_energy_mix(pd.DataFrame([record()]))
    featured = add_time_features(clean)
    assert {"hour", "day_of_week", "is_weekend", "consumption_lag_4"} <= set(featured.columns)


def test_missing_source_column_is_explicit():
    raw = pd.DataFrame([record()]).drop(columns="nucleaire")
    with pytest.raises(ValueError, match="nucleaire"):
        clean_energy_mix(raw)


def test_grid_mood_returns_explanation():
    clean = clean_energy_mix(pd.DataFrame([record()]))
    mood, reason = grid_mood(clean)
    assert mood in {"Calm", "Tense", "Carbon-heavy", "Renewable-rich"}
    assert reason

