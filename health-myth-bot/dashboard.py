"""
dashboard.py — Streamlit analytics dashboard for health-myth-bot.

Run with:
    streamlit run dashboard.py

Features
--------
- Password gate (DASHBOARD_PASSWORD env var) before any data is shown.
- Auto-refresh every 30 seconds.
- Summary metric cards (total queries, active languages, emergency flags, helpfulness rate).
- Plotly pie chart: query volume by language.
- Plotly horizontal bar chart: trending myths by category.
- Rumor-spike alert banner (stretch goal 4.4).
- Flagged myths review queue (stretch goal 4.5).
- Filter row: language, category, emergency-only toggle.
- Data table with emoji emergency badge and relative timestamps.
- CSV + PDF export (stretch goal 4.8).
- Footer crediting sources and confirming PII anonymisation.
"""

import os
import time
from datetime import datetime, timezone

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

import database  # noqa: E402
from alerting import detect_rumor_spike  # noqa: E402

# ---------------------------------------------------------------------------
# Page config — must be the very first Streamlit call
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Health Myth-Bot — Public Health Analytics",
    page_icon="🏥",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ---------------------------------------------------------------------------
# Color palette (mirrors static/index.html)
# ---------------------------------------------------------------------------
TEAL = "#1a7f7a"
DEEP_BLUE = "#1a3a5c"
OFF_WHITE = "#f5f7f9"
BORDER = "#d0d7de"
EMERGENCY_RED = "#c0392b"
ACCENT_AMBER = "#e67e22"
TEXT_DARK = "#1f2328"
TEXT_MUTED = "#57606a"
CHART_COLORS = [TEAL, DEEP_BLUE, "#2980b9", "#16a085", "#8e44ad", "#27ae60", ACCENT_AMBER]

# ---------------------------------------------------------------------------
# Custom CSS injection
# ---------------------------------------------------------------------------
CUSTOM_CSS = f"""
<style>
    /* ── Global ── */
    html, body, [class*="css"] {{
        font-family: -apple-system, "Segoe UI", system-ui, Arial, sans-serif;
        font-size: 15px;
        color: {TEXT_DARK};
    }}
    .stApp {{
        background-color: {OFF_WHITE};
    }}
    /* ── Metric cards ── */
    .metric-card {{
        background: #ffffff;
        border-radius: 8px;
        padding: 20px 24px 18px;
        box-shadow: 0 1px 4px rgba(0,0,0,0.08);
        border-top: 4px solid {TEAL};
        height: 100%;
    }}
    .metric-card.emergency {{
        border-top-color: {EMERGENCY_RED};
    }}
    .metric-card.helpfulness {{
        border-top-color: #27ae60;
    }}
    .metric-card .metric-label {{
        font-size: 12px;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        color: {TEXT_MUTED};
        margin-bottom: 6px;
    }}
    .metric-card .metric-value {{
        font-size: 36px;
        font-weight: 700;
        line-height: 1.1;
        color: {DEEP_BLUE};
    }}
    .metric-card.emergency .metric-value {{
        color: {EMERGENCY_RED};
    }}
    .metric-card.helpfulness .metric-value {{
        color: #27ae60;
    }}
    .metric-card .metric-sub {{
        font-size: 12px;
        color: {TEXT_MUTED};
        margin-top: 4px;
    }}
    /* ── Section headers ── */
    .section-header {{
        font-size: 18px;
        font-weight: 700;
        color: {DEEP_BLUE};
        margin: 28px 0 12px;
        padding-bottom: 6px;
        border-bottom: 2px solid {BORDER};
    }}
    /* ── Alert banner ── */
    .rumor-alert {{
        background: #fff3cd;
        border: 1.5px solid {ACCENT_AMBER};
        border-left: 6px solid {ACCENT_AMBER};
        border-radius: 6px;
        padding: 14px 18px;
        margin-bottom: 20px;
    }}
    .rumor-alert h4 {{
        margin: 0 0 6px;
        color: #7d5600;
        font-size: 15px;
    }}
    .rumor-alert p {{
        margin: 0;
        color: #7d5600;
        font-size: 13px;
    }}
    /* ── Live indicator ── */
    .live-dot {{
        display: inline-block;
        width: 9px;
        height: 9px;
        background: #27ae60;
        border-radius: 50%;
        margin-right: 5px;
        vertical-align: middle;
    }}
    /* ── Footer ── */
    .dash-footer {{
        font-size: 11px;
        color: {TEXT_MUTED};
        border-top: 1px solid {BORDER};
        padding-top: 14px;
        margin-top: 40px;
        text-align: center;
    }}
    /* ── Dataframe tweaks ── */
    .dataframe thead th {{
        background: {DEEP_BLUE} !important;
        color: white !important;
        font-size: 13px;
    }}
    /* Hide Streamlit branding ── */
    #MainMenu, footer, header {{visibility: hidden;}}
</style>
"""
st.markdown(CUSTOM_CSS, unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Password gate (stretch goal 4.7)
# ---------------------------------------------------------------------------
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")


def _check_auth() -> bool:
    if not DASHBOARD_PASSWORD:
        return True  # no password configured — open access
    if st.session_state.get("authenticated"):
        return True
    st.markdown(
        "<div style='max-width:380px;margin:80px auto;padding:36px;"
        f"background:#fff;border-radius:10px;box-shadow:0 2px 12px rgba(0,0,0,0.1);"
        f"border-top:5px solid {TEAL};'>"
        f"<h3 style='color:{DEEP_BLUE};margin-bottom:4px;'>🏥 Health Myth-Bot</h3>"
        f"<p style='color:{TEXT_MUTED};font-size:13px;margin-bottom:20px;'>Public Health Analytics — Authorised Access Only</p>"
        "</div>",
        unsafe_allow_html=True,
    )
    pwd = st.text_input("Password", type="password", placeholder="Enter dashboard password")
    if st.button("Sign in", type="primary"):
        if pwd == DASHBOARD_PASSWORD:
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("Incorrect password. Please try again.")
    return False


if not _check_auth():
    st.stop()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _relative_time(ts_str: str) -> str:
    """Convert a timestamp string to a human-readable relative string.

    Handles multiple formats that may exist in the database:
      - 'YYYY-MM-DD HH:MM:SS'          (current format, space separator)
      - 'YYYY-MM-DDTHH:MM:SS'          (old isoformat, T separator)
      - 'YYYY-MM-DDTHH:MM:SS+00:00'    (old isoformat with UTC offset)
      - 'YYYY-MM-DDTHH:MM:SS.ffffff+00:00'
    """
    if not ts_str:
        return "—"
    try:
        # Normalise: replace T separator and strip trailing UTC offset/Z
        cleaned = str(ts_str).replace("T", " ")
        # Strip +00:00 / -05:30 / Z suffixes
        for suffix_char in ("+", "Z"):
            if suffix_char in cleaned:
                cleaned = cleaned[:cleaned.index(suffix_char)]
        # Truncate microseconds if present (keep only HH:MM:SS)
        if "." in cleaned:
            cleaned = cleaned[:cleaned.index(".")]
        cleaned = cleaned.strip()
        ts = datetime.strptime(cleaned, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        diff = datetime.now(timezone.utc) - ts
        secs = int(diff.total_seconds())
        if secs < 0:
            return "just now"
        elif secs < 60:
            return f"{secs}s ago"
        elif secs < 3600:
            return f"{secs // 60}m ago"
        elif secs < 86400:
            return f"{secs // 3600}h ago"
        else:
            return f"{secs // 86400}d ago"
    except Exception:
        return str(ts_str)


def _load_data():
    stats = database.fetch_summary_stats()
    logs = database.fetch_all_logs(limit=500)
    alerts = database.fetch_recent_alerts(limit=10)
    flagged = database.fetch_flagged_myths(status="pending")
    return stats, logs, alerts, flagged


# ---------------------------------------------------------------------------
# Main dashboard render
# ---------------------------------------------------------------------------

def render_dashboard():
    # ── Header ──────────────────────────────────────────────────────────────
    col_title, col_live = st.columns([8, 2])
    with col_title:
        st.markdown(
            f"<h1 style='color:{DEEP_BLUE};margin-bottom:2px;'>🏥 Health Myth-Bot</h1>"
            f"<p style='color:{TEXT_MUTED};font-size:15px;margin-top:0;'>"
            "Real-time multilingual health myth monitoring</p>",
            unsafe_allow_html=True,
        )
    with col_live:
        now_str = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        st.markdown(
            f"<div style='text-align:right;padding-top:18px;'>"
            f"<span class='live-dot'></span>"
            f"<span style='color:#27ae60;font-size:13px;font-weight:600;'>LIVE</span>"
            f"<br><span style='color:{TEXT_MUTED};font-size:11px;'>Updated {now_str}</span>"
            "</div>",
            unsafe_allow_html=True,
        )

    # ── Load data ────────────────────────────────────────────────────────────
    stats, raw_logs, alerts, flagged = _load_data()
    df = pd.DataFrame(raw_logs) if raw_logs else pd.DataFrame(
        columns=["id", "anonymized_hash", "query_text", "language", "category",
                 "is_emergency", "timestamp"]
    )

    # ── Rumour-spike alert banner (stretch 4.4) ──────────────────────────────
    active_spikes = [a for a in alerts if a]
    if active_spikes:
        spike_text = ", ".join(
            f"<b>{a['category']}</b> ({a['spike_count']} queries vs {a['baseline_avg']:.1f} avg)"
            for a in active_spikes[:3]
        )
        st.markdown(
            f"<div class='rumor-alert'>"
            f"<h4>⚠️ Emerging Rumour Spike Detected</h4>"
            f"<p>Unusual query volume in: {spike_text}. "
            "Consider issuing a public health advisory.</p>"
            "</div>",
            unsafe_allow_html=True,
        )

    # ── Metric cards ────────────────────────────────────────────────────────
    st.markdown("<div class='section-header'>Overview</div>", unsafe_allow_html=True)
    c1, c2, c3, c4 = st.columns(4)

    helpfulness_display = (
        f"{stats['helpfulness_rate']}%" if stats["helpfulness_rate"] is not None else "N/A"
    )

    with c1:
        st.markdown(
            f"<div class='metric-card'>"
            f"<div class='metric-label'>Total Queries</div>"
            f"<div class='metric-value'>{stats['total_queries']:,}</div>"
            f"<div class='metric-sub'>All time</div>"
            "</div>",
            unsafe_allow_html=True,
        )
    with c2:
        st.markdown(
            f"<div class='metric-card'>"
            f"<div class='metric-label'>Active Languages</div>"
            f"<div class='metric-value'>{stats['active_languages']}</div>"
            f"<div class='metric-sub'>Unique language codes seen</div>"
            "</div>",
            unsafe_allow_html=True,
        )
    with c3:
        st.markdown(
            f"<div class='metric-card emergency'>"
            f"<div class='metric-label'>Emergency Flags</div>"
            f"<div class='metric-value'>{stats['emergency_count']}</div>"
            f"<div class='metric-sub'>Queries routed to emergency services</div>"
            "</div>",
            unsafe_allow_html=True,
        )
    with c4:
        st.markdown(
            f"<div class='metric-card helpfulness'>"
            f"<div class='metric-label'>Helpfulness Rate</div>"
            f"<div class='metric-value'>{helpfulness_display}</div>"
            f"<div class='metric-sub'>👍 feedback / total feedback</div>"
            "</div>",
            unsafe_allow_html=True,
        )

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Charts ───────────────────────────────────────────────────────────────
    if not df.empty:
        st.markdown("<div class='section-header'>Query Analytics</div>", unsafe_allow_html=True)
        ch1, ch2 = st.columns(2)

        with ch1:
            lang_counts = (
                df["language"]
                .value_counts()
                .rename_axis("Language")
                .reset_index(name="Queries")
            )
            lang_labels = {"en": "English", "hi": "Hindi", "sw": "Swahili"}
            lang_counts["Language"] = lang_counts["Language"].map(
                lambda x: lang_labels.get(x, x)
            )
            fig_pie = px.pie(
                lang_counts,
                names="Language",
                values="Queries",
                title="Query Volume by Language",
                color_discrete_sequence=CHART_COLORS,
                hole=0.35,
            )
            fig_pie.update_traces(textposition="inside", textinfo="percent+label")
            fig_pie.update_layout(
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                font=dict(family="-apple-system, Segoe UI, Arial, sans-serif", size=13),
                title_font_color=DEEP_BLUE,
                showlegend=True,
                legend=dict(orientation="h", yanchor="bottom", y=-0.2),
                margin=dict(t=50, b=20, l=10, r=10),
            )
            st.plotly_chart(fig_pie, use_container_width=True)

        with ch2:
            cat_counts = (
                df["category"]
                .value_counts()
                .rename_axis("Category")
                .reset_index(name="Queries")
                .sort_values("Queries", ascending=True)
            )
            # Emphasise the top category
            bar_colors = [ACCENT_AMBER if i == len(cat_counts) - 1 else TEAL
                          for i in range(len(cat_counts))]
            fig_bar = go.Figure(
                go.Bar(
                    x=cat_counts["Queries"],
                    y=cat_counts["Category"],
                    orientation="h",
                    marker_color=bar_colors,
                    hovertemplate="<b>%{y}</b><br>%{x} queries<extra></extra>",
                )
            )
            fig_bar.update_layout(
                title="Trending Rumors & Myths by Category",
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                font=dict(family="-apple-system, Segoe UI, Arial, sans-serif", size=13),
                title_font_color=DEEP_BLUE,
                xaxis=dict(title="Number of Queries", gridcolor=BORDER),
                yaxis=dict(title=""),
                margin=dict(t=50, b=20, l=10, r=10),
            )
            st.plotly_chart(fig_bar, use_container_width=True)

    # ── Filters ──────────────────────────────────────────────────────────────
    st.markdown("<div class='section-header'>Recent Queries</div>", unsafe_allow_html=True)

    if not df.empty:
        all_langs = sorted(df["language"].dropna().unique().tolist())
        all_cats = sorted(df["category"].dropna().unique().tolist())

        f1, f2, f3 = st.columns([3, 3, 2])
        with f1:
            sel_langs = st.multiselect(
                "Filter by language",
                options=all_langs,
                default=all_langs,
                format_func=lambda x: {"en": "English", "hi": "Hindi", "sw": "Swahili"}.get(x, x),
            )
        with f2:
            sel_cats = st.multiselect(
                "Filter by category",
                options=all_cats,
                default=all_cats,
            )
        with f3:
            emergency_only = st.toggle("🚨 Emergency only", value=False)

        # Apply filters (client-side — no additional DB calls)
        filtered_df = df.copy()
        if sel_langs:
            filtered_df = filtered_df[filtered_df["language"].isin(sel_langs)]
        if sel_cats:
            filtered_df = filtered_df[filtered_df["category"].isin(sel_cats)]
        if emergency_only:
            filtered_df = filtered_df[filtered_df["is_emergency"] == 1]

        # Build display dataframe
        display_df = filtered_df.head(50).copy()
        display_df["🚨"] = display_df["is_emergency"].apply(
            lambda v: "🚨 Emergency" if v else ""
        )
        display_df["When"] = display_df["timestamp"].apply(_relative_time)
        display_df["Language"] = display_df["language"].map(
            {"en": "English", "hi": "Hindi", "sw": "Swahili"}
        ).fillna(display_df["language"])

        display_cols = {
            "anonymized_hash": "User Hash",
            "query_text": "Query",
            "Language": "Language",
            "category": "Category",
            "🚨": "Alert",
            "When": "When",
        }
        st.dataframe(
            display_df.rename(columns=display_cols)[list(display_cols.values())],
            use_container_width=True,
            height=380,
        )

        # ── Export (stretch goal 4.8) ─────────────────────────────────────────
        st.markdown("<br>", unsafe_allow_html=True)
        exp1, exp2, _ = st.columns([2, 2, 6])
        with exp1:
            csv_data = filtered_df[
                ["anonymized_hash", "query_text", "language", "category", "is_emergency", "timestamp"]
            ].to_csv(index=False).encode("utf-8")
            st.download_button(
                "📥 Export CSV",
                data=csv_data,
                file_name="health_myth_bot_logs.csv",
                mime="text/csv",
            )
        with exp2:
            if st.button("📄 Download Weekly PDF"):
                pdf_bytes = _generate_pdf_report(stats, filtered_df, active_spikes)
                st.download_button(
                    "⬇️ Save PDF",
                    data=pdf_bytes,
                    file_name="health_myth_bot_weekly_report.pdf",
                    mime="application/pdf",
                )
    else:
        st.info("No query data yet. Run `python seed_data.py` to populate sample data.")

    # ── Flagged myths review queue (stretch 4.5) ────────────────────────────
    if flagged:
        st.markdown(
            "<div class='section-header'>🔍 Flagged Myths — Pending Review</div>",
            unsafe_allow_html=True,
        )
        flagged_df = pd.DataFrame(flagged)
        flagged_df["When"] = flagged_df["timestamp"].apply(_relative_time)
        st.dataframe(
            flagged_df[["id", "query_text", "language", "When", "status"]].rename(
                columns={
                    "id": "ID", "query_text": "Reported Query",
                    "language": "Language", "status": "Status",
                }
            ),
            use_container_width=True,
            height=220,
        )

    # ── Footer ───────────────────────────────────────────────────────────────
    st.markdown(
        "<div class='dash-footer'>"
        "Data sources: WHO, Ministry of Health guidelines, verified health literature. "
        "All user identifiers shown are anonymised SHA-256 hashes — no phone numbers are stored or displayed. "
        "This dashboard is intended for authorised public health officials only."
        "</div>",
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# PDF report generator (stretch goal 4.8)
# ---------------------------------------------------------------------------

def _generate_pdf_report(stats: dict, df: pd.DataFrame, spikes: list) -> bytes:
    """Generate a simple weekly summary PDF using fpdf2."""
    try:
        from fpdf import FPDF  # noqa: PLC0415

        pdf = FPDF()
        pdf.add_page()
        pdf.set_font("Helvetica", "B", 18)
        pdf.set_text_color(26, 58, 92)  # DEEP_BLUE
        pdf.cell(0, 12, "Health Myth-Bot — Weekly Summary Report", ln=True)

        pdf.set_font("Helvetica", "", 10)
        pdf.set_text_color(87, 96, 106)
        pdf.cell(
            0, 7,
            f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
            ln=True,
        )
        pdf.ln(6)

        # Stats table
        pdf.set_font("Helvetica", "B", 13)
        pdf.set_text_color(26, 58, 92)
        pdf.cell(0, 9, "Summary Statistics", ln=True)
        pdf.set_font("Helvetica", "", 11)
        pdf.set_text_color(31, 35, 40)
        rows = [
            ("Total Queries", str(stats.get("total_queries", 0))),
            ("Active Languages", str(stats.get("active_languages", 0))),
            ("Emergency Flags", str(stats.get("emergency_count", 0))),
            ("Helpfulness Rate", f"{stats.get('helpfulness_rate') or 'N/A'}%"),
        ]
        for label, val in rows:
            pdf.cell(80, 8, label, border="B")
            pdf.cell(0, 8, val, border="B", ln=True)
        pdf.ln(6)

        # Top categories
        if not df.empty:
            pdf.set_font("Helvetica", "B", 13)
            pdf.set_text_color(26, 58, 92)
            pdf.cell(0, 9, "Top Categories (Trending Myths)", ln=True)
            pdf.set_font("Helvetica", "", 11)
            pdf.set_text_color(31, 35, 40)
            top_cats = df["category"].value_counts().head(5)
            for cat, count in top_cats.items():
                pdf.cell(80, 8, str(cat), border="B")
                pdf.cell(0, 8, str(count), border="B", ln=True)
            pdf.ln(6)

        # Spike alerts
        if spikes:
            pdf.set_font("Helvetica", "B", 13)
            pdf.set_text_color(192, 57, 43)
            pdf.cell(0, 9, "⚠️ Rumour Spike Alerts", ln=True)
            pdf.set_font("Helvetica", "", 11)
            pdf.set_text_color(31, 35, 40)
            for spike in spikes[:5]:
                line = (
                    f"{spike.get('category', '?')}: "
                    f"{spike.get('spike_count', '?')} queries "
                    f"(baseline avg: {spike.get('baseline_avg', '?')})"
                )
                pdf.cell(0, 8, line, ln=True)
            pdf.ln(6)

        pdf.set_font("Helvetica", "I", 9)
        pdf.set_text_color(87, 96, 106)
        pdf.cell(
            0, 8,
            "All identifiers shown are anonymised hashes. No personal data is included.",
            ln=True,
        )

        return pdf.output(dest="S").encode("latin-1")

    except ImportError:
        # fpdf2 not installed — return a minimal plain-text placeholder
        text = (
            f"Health Myth-Bot Weekly Report\n"
            f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n\n"
            f"Total Queries: {stats.get('total_queries', 0)}\n"
            f"Emergency Flags: {stats.get('emergency_count', 0)}\n\n"
            "(Install fpdf2 for full PDF formatting)\n"
        )
        return text.encode("utf-8")
    except Exception as exc:  # pylint: disable=broad-except
        return f"PDF generation error: {exc}".encode("utf-8")


# ---------------------------------------------------------------------------
# Auto-refresh loop
# ---------------------------------------------------------------------------

render_dashboard()
time.sleep(30)
st.rerun()
