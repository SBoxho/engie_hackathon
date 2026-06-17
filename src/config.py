from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    project_root: Path = PROJECT_ROOT
    odre_base_url: str = os.getenv(
        "ODRE_BASE_URL", "https://odre.opendatasoft.com/api/explore/v2.1"
    )
    open_meteo_base_url: str = os.getenv(
        "OPEN_METEO_BASE_URL", "https://api.open-meteo.com/v1/forecast"
    )
    entsoe_api_token: str | None = os.getenv("ENTSOE_API_TOKEN") or None
    timezone: str = os.getenv("ENERGY_PULSE_TIMEZONE", "Europe/Paris")
    history_hours: int = int(os.getenv("ENERGY_PULSE_HISTORY_HOURS", "72"))

    @property
    def raw_dir(self) -> Path:
        return self.project_root / "data" / "raw"

    @property
    def processed_dir(self) -> Path:
        return self.project_root / "data" / "processed"

    @property
    def energy_store_dir(self) -> Path:
        return self.processed_dir / "eco2mix"

    @property
    def weather_features_path(self) -> Path:
        return self.processed_dir / "weather_national.parquet"

    @property
    def joined_features_path(self) -> Path:
        return self.processed_dir / "energy_weather.parquet"

    @property
    def baseline_artifact_path(self) -> Path:
        return self.processed_dir / "baseline_backtest.json"

    @property
    def mood_artifact_path(self) -> Path:
        return self.processed_dir / "mood_calibration.json"


settings = Settings()
