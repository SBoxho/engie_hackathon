import streamlit as st


def apply_theme() -> None:
    st.markdown(
        """
        <style>
        .stApp {background: radial-gradient(circle at 80% 0%, #143044 0, #08131f 34%, #050b12 72%);}
        [data-testid="stMetric"] {background: rgba(18,35,49,.78); border: 1px solid rgba(94,234,212,.18);
          padding: 1.05rem; border-radius: 16px; box-shadow: 0 14px 35px rgba(0,0,0,.2);}
        [data-testid="stMetricValue"] {color: #f4fbff;}
        .eyebrow {color:#5eead4; text-transform:uppercase; letter-spacing:.16em; font-size:.74rem; font-weight:700;}
        .hero {font-size:3rem; font-weight:760; line-height:1; margin:.4rem 0 .6rem;
          background:linear-gradient(90deg,#f8fafc,#5eead4); -webkit-background-clip:text; color:transparent;}
        .subtitle {color:#a7bac8; font-size:1.08rem; margin-bottom:1.6rem;}
        .status {display:inline-block; padding:.28rem .65rem; border-radius:999px; background:rgba(94,234,212,.1);
          color:#5eead4; border:1px solid rgba(94,234,212,.25); font-size:.78rem;}
        div[data-testid="stPlotlyChart"] {background:rgba(9,22,34,.55); border:1px solid rgba(148,163,184,.12); border-radius:16px;}
        </style>
        """,
        unsafe_allow_html=True,
    )

