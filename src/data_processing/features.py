from __future__ import annotations

import pandas as pd


def add_time_features(frame: pd.DataFrame, timezone: str = "Europe/Paris") -> pd.DataFrame:
    result = frame.copy()
    local = pd.to_datetime(result["timestamp"], utc=True).dt.tz_convert(timezone)
    result["hour"] = local.dt.hour
    result["day_of_week"] = local.dt.dayofweek
    result["month"] = local.dt.month
    result["is_weekend"] = result["day_of_week"].ge(5)
    try:
        import holidays

        french_holidays = holidays.France(years=sorted(local.dt.year.unique()))
        result["is_holiday"] = local.dt.date.map(lambda day: day in french_holidays)
    except ImportError:
        result["is_holiday"] = False
    for periods in (4, 12, 24, 96):
        result[f"consumption_lag_{periods}"] = result["consumption_mw"].shift(periods)
    result["consumption_rolling_4h"] = result["consumption_mw"].rolling(16).mean()
    result["consumption_rolling_24h"] = result["consumption_mw"].rolling(96).mean()
    return result

