from __future__ import annotations

import streamlit as st


def metric_card(label: str, value: str, help_text: str | None = None) -> None:
    st.metric(label, value, help=help_text)

