"""Reusable Streamlit quality panel; no page registration side effects."""
from __future__ import annotations

import pandas as pd

from src.data_processing.quality import QualityReport, run_quality_checks


def render_data_quality(frame: pd.DataFrame, report: QualityReport | None = None) -> QualityReport:
    """Render quality status, classified findings, and suspicious-row evidence."""
    import streamlit as st

    report = report or run_quality_checks(frame)
    st.subheader("Data quality")
    errors = sum(f.severity == "error" for f in report.findings)
    warnings = sum(f.severity == "warning" for f in report.findings)
    col1, col2, col3 = st.columns(3)
    col1.metric("Rows checked", report.rows_checked)
    col2.metric("Errors", errors)
    col3.metric("Warnings", warnings)
    st.dataframe(report.summary_frame(), width="stretch", hide_index=True)
    if report.suspicious_rows.empty:
        st.success("No suspicious rows were identified by the configured checks.")
    else:
        st.warning(f"{len(report.suspicious_rows)} check-row evidence records require review.")
        st.dataframe(report.suspicious_rows, width="stretch", hide_index=True)
    return report
