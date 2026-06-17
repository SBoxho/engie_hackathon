"""Evidence-preserving data-quality checks for processed energy data."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

import pandas as pd

KEY_COLUMNS = ("timestamp", "region")
GENERATION_COLUMNS = (
    "nuclear_mw", "wind_mw", "solar_mw", "hydro_mw", "gas_mw",
    "coal_mw", "oil_mw", "bioenergy_mw", "total_production_mw",
)
REQUIRED_COLUMNS = (
    "timestamp", "region", "consumption_mw", "nuclear_mw", "wind_mw",
    "solar_mw", "hydro_mw", "gas_mw", "coal_mw", "oil_mw",
    "bioenergy_mw", "imports_mw", "exports_mw", "total_production_mw",
    "renewable_share", "fossil_share",
)


@dataclass(frozen=True)
class QualityFinding:
    check: str
    classification: str
    severity: str
    count: int
    message: str


@dataclass(frozen=True)
class QualityConfig:
    cadence: str = "15min"
    null_warning_fraction: float = 0.05
    stale_after: str = "90min"
    balance_absolute_tolerance_mw: float = 1_000.0
    balance_relative_tolerance: float = 0.05
    maximum_power_mw: float = 200_000.0
    maximum_co2_g_per_kwh: float = 2_000.0


@dataclass
class QualityReport:
    findings: list[QualityFinding]
    suspicious_rows: pd.DataFrame
    rows_checked: int
    generated_at: datetime

    @property
    def passed(self) -> bool:
        return not any(f.severity == "error" for f in self.findings)

    def summary_frame(self) -> pd.DataFrame:
        return pd.DataFrame([asdict(f) for f in self.findings])

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "rows_checked": self.rows_checked,
            "generated_at": self.generated_at.isoformat(),
            "findings": [asdict(f) for f in self.findings],
            "suspicious_row_count": len(self.suspicious_rows),
        }


class _Collector:
    def __init__(self, original: pd.DataFrame) -> None:
        self.original = original
        self.findings: list[QualityFinding] = []
        self.evidence: list[pd.DataFrame] = []

    def add(
        self,
        check: str,
        classification: str,
        severity: str,
        mask: pd.Series | Iterable[bool] | None,
        message: str,
        *,
        count: int | None = None,
    ) -> None:
        selected = pd.Series(mask, index=self.original.index).fillna(False).astype(bool) if mask is not None else None
        finding_count = int(selected.sum()) if count is None and selected is not None else int(count or 0)
        self.findings.append(QualityFinding(check, classification, severity, finding_count, message))
        if selected is not None and selected.any():
            rows = self.original.loc[selected].copy()
            rows.insert(0, "_source_index", rows.index)
            rows.insert(0, "_quality_severity", severity)
            rows.insert(0, "_quality_check", check)
            self.evidence.append(rows.reset_index(drop=True))


def run_quality_checks(
    frame: pd.DataFrame,
    *,
    now: datetime | pd.Timestamp | None = None,
    config: QualityConfig | None = None,
    required_columns: Iterable[str] = REQUIRED_COLUMNS,
) -> QualityReport:
    """Classify quality failures without modifying or dropping input rows."""
    cfg = config or QualityConfig()
    checked_at = pd.Timestamp(now or datetime.now(timezone.utc))
    if checked_at.tzinfo is None:
        checked_at = checked_at.tz_localize("UTC")
    else:
        checked_at = checked_at.tz_convert("UTC")
    source = frame.copy()
    collector = _Collector(source)

    missing = sorted(set(required_columns).difference(source.columns))
    collector.add(
        "schema", "schema", "error" if missing else "info", None,
        f"Missing required columns: {missing}" if missing else "Required schema is present.",
        count=len(missing),
    )

    if "timestamp" in source:
        timestamps = pd.to_datetime(source["timestamp"], utc=True, errors="coerce")
        invalid_ts = timestamps.isna()
        collector.add(
            "timestamp_validity", "timestamp", "error" if invalid_ts.any() else "info",
            invalid_ts, "Timestamps must be parseable, non-null instants.",
        )
    else:
        timestamps = pd.Series(pd.NaT, index=source.index, dtype="datetime64[ns, UTC]")

    if all(column in source for column in KEY_COLUMNS):
        duplicate_mask = source.duplicated(list(KEY_COLUMNS), keep=False)
        collector.add(
            "duplicate_keys", "duplicates_missing", "error" if duplicate_mask.any() else "info",
            duplicate_mask, "Duplicate timestamp+region keys are reported; no rows were removed.",
        )

        valid = pd.DataFrame({"timestamp": timestamps, "region": source["region"]}, index=source.index).dropna()
        expected = pd.Timedelta(cfg.cadence)
        cadence_mask = pd.Series(False, index=source.index)
        missing_intervals = 0
        for _, group in valid.sort_values("timestamp").groupby("region"):
            unique = group.drop_duplicates("timestamp").sort_values("timestamp")
            deltas = unique["timestamp"].diff()
            bad = deltas.notna() & (deltas != expected)
            cadence_mask.loc[unique.index[bad]] = True
            gaps = deltas[deltas > expected]
            missing_intervals += int(sum(max(int(delta / expected) - 1, 0) for delta in gaps))
        collector.add(
            "cadence", "cadence", "warning" if cadence_mask.any() else "info", cadence_mask,
            f"Per-region cadence must be {cfg.cadence}; rows after irregular intervals are evidence.",
        )
        collector.add(
            "missing_intervals", "duplicates_missing", "warning" if missing_intervals else "info",
            cadence_mask, f"Estimated missing {cfg.cadence} intervals between observations.",
            count=missing_intervals,
        )

    measured = [c for c in source.columns if c not in KEY_COLUMNS]
    for column in measured:
        fraction = float(source[column].isna().mean()) if len(source) else 0.0
        mask = source[column].isna()
        collector.add(
            f"null_fraction:{column}", "null_percentage",
            "warning" if fraction > cfg.null_warning_fraction else "info", mask,
            f"{column} null fraction is {fraction:.1%} (warning above {cfg.null_warning_fraction:.1%}).",
        )

    available_generation = [c for c in GENERATION_COLUMNS if c in source]
    if available_generation:
        numeric_generation = source[available_generation].apply(pd.to_numeric, errors="coerce")
        negative = numeric_generation.lt(0).any(axis=1)
        collector.add(
            "nonnegative_generation", "physical_constraint", "error" if negative.any() else "info",
            negative, "Generation values must be nonnegative.",
        )

    share_columns = [c for c in ("renewable_share", "fossil_share") if c in source]
    if share_columns:
        shares = source[share_columns].apply(pd.to_numeric, errors="coerce")
        invalid_share = ((shares < 0) | (shares > 1)).any(axis=1)
        if len(share_columns) == 2:
            invalid_share |= shares.sum(axis=1, min_count=2) > 1.000001
        collector.add(
            "share_bounds", "physical_constraint", "error" if invalid_share.any() else "info",
            invalid_share, "Shares must be in [0, 1] and renewable+fossil cannot exceed 1.",
        )

    valid_timestamps = timestamps.dropna()
    stale = valid_timestamps.empty or checked_at - valid_timestamps.max() > pd.Timedelta(cfg.stale_after)
    stale_mask = pd.Series(False, index=source.index)
    if stale and not valid_timestamps.empty:
        stale_mask.loc[valid_timestamps.idxmax()] = True
    collector.add(
        "stale_latest_timestamp", "freshness", "warning" if stale else "info", stale_mask,
        f"Latest valid timestamp must be no older than {cfg.stale_after}.", count=int(stale),
    )

    extreme_mask = pd.Series(False, index=source.index)
    power_columns = [c for c in source.columns if c.endswith("_mw")]
    if power_columns:
        extreme_mask |= source[power_columns].apply(pd.to_numeric, errors="coerce").abs().gt(
            cfg.maximum_power_mw
        ).any(axis=1)
    if "co2_intensity_g_per_kwh" in source:
        co2 = pd.to_numeric(source["co2_intensity_g_per_kwh"], errors="coerce")
        extreme_mask |= co2.lt(0) | co2.gt(cfg.maximum_co2_g_per_kwh)
    collector.add(
        "extreme_values", "extremes", "warning" if extreme_mask.any() else "info",
        extreme_mask, "Values exceed broad physical plausibility limits and require review.",
    )

    balance_columns = {"total_production_mw", "imports_mw", "exports_mw", "consumption_mw"}
    if balance_columns <= set(source.columns):
        values = source[list(balance_columns)].apply(pd.to_numeric, errors="coerce")
        residual = (
            values["total_production_mw"] + values["imports_mw"]
            - values["exports_mw"] - values["consumption_mw"]
        )
        tolerance = values["consumption_mw"].abs().mul(cfg.balance_relative_tolerance).clip(
            lower=cfg.balance_absolute_tolerance_mw
        )
        imbalance = residual.abs() > tolerance
        collector.add(
            "balance_residual", "balance", "warning" if imbalance.any() else "info", imbalance,
            "Absolute supply-demand residual exceeds max(absolute tolerance, relative demand tolerance).",
        )

    evidence = pd.concat(collector.evidence, ignore_index=True, sort=False) if collector.evidence else pd.DataFrame()
    return QualityReport(collector.findings, evidence, len(source), checked_at.to_pydatetime())
