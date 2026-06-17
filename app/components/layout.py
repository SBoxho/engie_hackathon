import streamlit as st


def apply_theme() -> None:
    st.markdown(
        """
        <style>
        .stApp {background:
          radial-gradient(circle at 84% 0%, rgba(20,48,68,.94) 0, rgba(8,19,31,.97) 35%, #050b12 72%);}
        section[data-testid="stSidebar"] {background: #07111c;}
        .block-container {padding-top: 2.2rem; max-width: 1180px;}
        [data-testid="stMetric"] {background: rgba(18,35,49,.78); border: 1px solid rgba(94,234,212,.18);
          padding: 1.05rem; border-radius: 8px; box-shadow: 0 14px 35px rgba(0,0,0,.2);}
        [data-testid="stMetricValue"] {color: #f4fbff;}
        .eyebrow {color:#5eead4; text-transform:uppercase; letter-spacing:.16em; font-size:.74rem; font-weight:700;}
        .hero {font-size: clamp(2.6rem, 7vw, 5.6rem); font-weight:800; line-height:.95; margin:.35rem 0 .75rem;
          color:#f8fafc; letter-spacing:0;}
        .subtitle {color:#c8d8e4; font-size:1.18rem; max-width:720px; margin-bottom:1.6rem;}
        .status {display:inline-block; padding:.28rem .65rem; border-radius:999px; background:rgba(94,234,212,.1);
          color:#5eead4; border:1px solid rgba(94,234,212,.25); font-size:.78rem;}
        div[data-testid="stPlotlyChart"] {background:rgba(9,22,34,.55); border:1px solid rgba(148,163,184,.12); border-radius:8px;}
        h2, h3 {letter-spacing:0;}
        .section-kicker {color:#8be7dd; font-size:.78rem; font-weight:800; letter-spacing:.14em;
          text-transform:uppercase; margin:1.4rem 0 .2rem;}
        .section-title {font-size:1.65rem; font-weight:780; color:#f8fafc; margin:0 0 .35rem;}
        .section-copy {color:#aabaca; font-size:1rem; margin:0 0 1rem; max-width:760px;}
        .pulse-card, .driver-card, .honesty-card, .action-panel {border:1px solid rgba(148,163,184,.17);
          background:linear-gradient(180deg, rgba(17,34,50,.86), rgba(8,19,31,.88));
          border-radius:8px; padding:1rem; min-height:132px; box-shadow:0 16px 38px rgba(0,0,0,.18);}
        .pulse-icon, .driver-icon {width:38px; height:38px; border-radius:8px; display:flex; align-items:center;
          justify-content:center; background:rgba(94,234,212,.11); color:#64f4e5; border:1px solid rgba(94,234,212,.28);
          font-size:1.2rem; margin-bottom:.75rem;}
        .pulse-label, .driver-label {color:#90a4b8; font-size:.78rem; font-weight:760; text-transform:uppercase;
          letter-spacing:.08em;}
        .pulse-value {color:#f8fafc; font-size:1.55rem; font-weight:820; line-height:1.1; margin:.25rem 0 .35rem;}
        .pulse-detail, .driver-detail, .honesty-detail {color:#9fb2c3; font-size:.92rem; line-height:1.4;}
        .driver-card {min-height:156px;}
        .driver-title {color:#f8fafc; font-size:1.08rem; font-weight:760; margin:.25rem 0 .45rem;}
        .honesty-card {min-height:auto;}
        .honesty-good {color:#6ee7b7; font-weight:800;}
        .honesty-watch {color:#fbbf24; font-weight:800;}
        .action-panel {background:linear-gradient(135deg, rgba(13,148,136,.22), rgba(8,19,31,.92)); min-height:auto;}
        .action-title {font-size:1.25rem; color:#f8fafc; font-weight:800; margin-bottom:.35rem;}
        .small-muted {color:#91a5b5; font-size:.86rem;}
        div[data-testid="stLinkButton"] a, div[data-testid="stPageLink"] a {border-radius:8px;}
        </style>
        """,
        unsafe_allow_html=True,
    )
