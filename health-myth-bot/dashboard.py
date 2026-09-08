"""
dashboard.py — Streamlit analytics dashboard for health-myth-bot.

Run with:
    streamlit run dashboard.py

Features
--------
- Password gate (DASHBOARD_PASSWORD env var) before any data is shown.
- Minimal dark theme (paired with .streamlit/config.toml).
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
# Dark color palette (mirrors .streamlit/config.toml)
# ---------------------------------------------------------------------------
ACCENT_BLUE = "#6b93c9"         # primary accent (soft light blue)
DEEP_BLUE = "#dde2ec"           # heading / emphasis text (light on dark)
OFF_WHITE = "#0e1116"           # page background (deep charcoal)
BORDER = "#282f3d"              # hairline borders / gridlines
EMERGENCY_RED = "#e08585"
ACCENT_AMBER = "#d9a659"
TEXT_DARK = "#dde2ec"           # main text (light on dark)
TEXT_MUTED = "#8b93a7"          # secondary text
SUCCESS_GREEN = "#6fbf9c"
CARD_BG = "#161b24"             # raised card surface
CHART_COLORS = ["#6b93c9", "#9a8cc9", "#6fbf9c", "#c98ca8", "#d9a659", "#e08585"]
PLOT_FONT = "-apple-system, Segoe UI, system-ui, Arial, sans-serif"

# ---------------------------------------------------------------------------
# Custom CSS injection
# ---------------------------------------------------------------------------
CUSTOM_CSS = f"""
<style>
    /* ── Global ── */
    html, body, [class*="css"], .stApp {{
        font-family: -apple-system, "Segoe UI", system-ui, Arial, sans-serif;
        color: {TEXT_DARK} !important;
    }}
    .stApp {{
        background-color: {OFF_WHITE};
    }}
    h1, h2, h3, h4, h5, h6, p, span, label, div {{
        color: inherit;
    }}
    /* Force legible text on Streamlit's own widgets in dark mode */
    .stApp, .stApp * {{
        scrollbar-color: {BORDER} transparent;
    }}
    .stMarkdown, .stMarkdown * {{
        color: {TEXT_DARK};
    }}
    label, .stTextInput label, .stMultiSelect label, .stToggle label {{
        color: {TEXT_MUTED} !important;
        font-size: 13px;
        font-weight: 500;
    }}
    .stTextInput input {{
        background-color: {CARD_BG} !important;
        color: {TEXT_DARK} !important;
        border: 1px solid {BORDER} !important;
        border-radius: 8px;
    }}
    .stTextInput input::placeholder {{
        color: {TEXT_MUTED} !important;
    }}
    /* ── Filter tags: light blue with dark text for contrast ── */
    [data-testid="stMultiSelectTagsContainer"] span[data-tag] {{
        background-color: #8fb3e0 !important;
        border-radius: 6px;
    }}
    [data-testid="stMultiSelectTagsContainer"] span[data-tag],
    [data-testid="stMultiSelectTagsContainer"] span[data-tag] span,
    [data-testid="stMultiSelectTagsContainer"] span[data-tag] svg {{
        color: #10141c !important;
    }}
    /* Primary buttons: dark text on the light-blue accent */
    section.stMain button[kind="primary"] {{
        color: #0f131a !important;
    }}
    /* ── Metric cards ── */
    .metric-card {{
        background: {CARD_BG};
        border: 1px solid {BORDER};
        border-radius: 12px;
        padding: 20px 22px 18px;
        height: 100%;
    }}
    .metric-card .metric-label {{
        font-size: 11px;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        color: {TEXT_MUTED};
        margin-bottom: 8px;
    }}
    .metric-card .metric-value {{
        font-size: 34px;
        font-weight: 700;
        line-height: 1.1;
        color: {TEXT_DARK};
    }}
    .metric-card .metric-sub {{
        font-size: 12px;
        color: {TEXT_MUTED};
        margin-top: 6px;
    }}
    .metric-accent {{
        display: inline-block;
        width: 26px;
        height: 3px;
        border-radius: 2px;
        margin-bottom: 12px;
        background: {ACCENT_BLUE};
    }}
    .metric-card.emergency .metric-accent {{ background: {EMERGENCY_RED}; }}
    .metric-card.emergency .metric-value {{ color: {EMERGENCY_RED}; }}
    .metric-card.helpfulness .metric-accent {{ background: {SUCCESS_GREEN}; }}
    .metric-card.helpfulness .metric-value {{ color: {SUCCESS_GREEN}; }}
    /* ── Section headers ── */
    .section-header {{
        font-size: 15px;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.06em;
        color: {TEXT_MUTED};
        margin: 30px 0 14px;
    }}
    /* ── Alert banner ── */
    .rumor-alert {{
        background: rgba(217, 166, 89, 0.07);
        border: 1px solid rgba(217, 166, 89, 0.3);
        border-left: 4px solid {ACCENT_AMBER};
        border-radius: 10px;
        padding: 14px 18px;
        margin-bottom: 20px;
    }}
    .rumor-alert h4 {{
        margin: 0 0 6px;
        color: {ACCENT_AMBER};
        font-size: 15px;
    }}
    .rumor-alert p {{
        margin: 0;
        color: {TEXT_MUTED};
        font-size: 13px;
    }}
    .rumor-alert p b, .rumor-alert strong {{
        color: {TEXT_DARK};
    }}
    /* ── Live indicator ── */
    .live-dot {{
        display: inline-block;
        width: 8px;
        height: 8px;
        background: {SUCCESS_GREEN};
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
    /* Hide Streamlit branding ── */
    #MainMenu, footer, header {{visibility: hidden;}}
</style>
"""
st.markdown(CUSTOM_CSS, unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Login screen (stretch goal 4.7)
# ---------------------------------------------------------------------------
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")

_LOGIN_CSS = f"""
<style>
    /* Fixed-width login card. Streamlit renders forms as div[data-testid="stForm"]
       (never a nested <form>), so style that element directly. */
    section.stMain div[data-testid="stForm"] {{
        width: 340px;              /* fixed-length card, not screen-wide */
        max-width: 90vw;
        box-sizing: border-box;
        margin: 0 auto;            /* centre within the block container */
        padding: 22px 22px 16px;
        background: {CARD_BG};
        border: 1px solid {BORDER};
        border-radius: 14px;
    }}
    .login-head {{
        text-align: center;
        margin: 0 0 4px;
    }}
    .login-logo {{
        font-size: 32px;
        line-height: 1;
    }}
    .login-title {{
        font-size: 19px;
        font-weight: 700;
        color: {TEXT_DARK};
        margin: 10px 0 4px;
    }}
    .login-sub {{
        font-size: 13px;
        color: {TEXT_MUTED};
        margin: 0;
    }}
    .login-hint {{
        font-size: 11.5px;
        color: {TEXT_MUTED};
        text-align: center;
        width: 340px;
        max-width: 90vw;
        margin: 14px auto 0;
    }}
</style>
"""


def _check_auth() -> bool:
    if not DASHBOARD_PASSWORD:
        return True  # no password configured — open access
    if st.session_state.get("authenticated"):
        return True

    st.markdown(_LOGIN_CSS, unsafe_allow_html=True)
    st.markdown(
        f"<div class='login-head'>"
        f"<div class='login-logo'>🏥</div>"
        f"<div class='login-title'>Health Myth-Bot</div>"
        f"<div class='login-sub'>Public Health Analytics</div>"
        "</div>",
        unsafe_allow_html=True,
    )
    with st.form("login_form", clear_on_submit=False):
        pwd = st.text_input(
            "Password",
            type="password",
            placeholder="Enter dashboard password",
            label_visibility="collapsed",
        )
        submitted = st.form_submit_button("Sign in", width="stretch")
        if submitted:
            if pwd == DASHBOARD_PASSWORD:
                st.session_state["authenticated"] = True
                st.rerun()
            else:
                st.error("Incorrect password. Please try again.")
    st.markdown(
        "<div class='login-hint'>Authorised public health officials only · "
        "Access is logged</div>",
        unsafe_allow_html=True,
    )
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
            f"<h1 style='color:{TEXT_DARK};margin-bottom:2px;font-size:28px;'>🏥 Health Myth-Bot</h1>"
            f"<p style='color:{TEXT_MUTED};font-size:14px;margin-top:0;'>"
            "Real-time multilingual health myth monitoring</p>",
            unsafe_allow_html=True,
        )
    with col_live:
        now_str = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        st.markdown(
            f"<div style='text-align:right;padding-top:18px;'>"
            f"<span class='live-dot'></span>"
            f"<span style='color:{SUCCESS_GREEN};font-size:12px;font-weight:600;'>LIVE</span>"
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
            f"<span class='metric-accent'></span>"
            f"<div class='metric-label'>Total Queries</div>"
            f"<div class='metric-value'>{stats['total_queries']:,}</div>"
            f"<div class='metric-sub'>All time</div>"
            "</div>",
            unsafe_allow_html=True,
        )
    with c2:
        st.markdown(
            f"<div class='metric-card'>"
            f"<span class='metric-accent'></span>"
            f"<div class='metric-label'>Active Languages</div>"
            f"<div class='metric-value'>{stats['active_languages']}</div>"
            f"<div class='metric-sub'>Unique language codes seen</div>"
            "</div>",
            unsafe_allow_html=True,
        )
    with c3:
        st.markdown(
            f"<div class='metric-card emergency'>"
            f"<span class='metric-accent'></span>"
            f"<div class='metric-label'>Emergency Flags</div>"
            f"<div class='metric-value'>{stats['emergency_count']}</div>"
            f"<div class='metric-sub'>Queries routed to emergency services</div>"
            "</div>",
            unsafe_allow_html=True,
        )
    with c4:
        st.markdown(
            f"<div class='metric-card helpfulness'>"
            f"<span class='metric-accent'></span>"
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
                hole=0.4,
            )
            fig_pie.update_traces(
                textposition="inside",
                textinfo="percent+label",
                textfont_color="#0f131a",
                marker_line_color=CARD_BG,
                marker_line_width=2,
            )
            fig_pie.update_layout(
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                font=dict(family=PLOT_FONT, size=13, color=TEXT_DARK),
                title_font_color=TEXT_DARK,
                hoverlabel=dict(font_color=TEXT_DARK, bgcolor=CARD_BG),
                showlegend=True,
                legend=dict(orientation="h", yanchor="bottom", y=-0.15),
                margin=dict(t=50, b=20, l=10, r=10),
            )
            st.plotly_chart(fig_pie, width="stretch")

        with ch2:
            cat_counts = (
                df["category"]
                .value_counts()
                .rename_axis("Category")
                .reset_index(name="Queries")
                .sort_values("Queries", ascending=True)
            )
            # Emphasise the top category
            bar_colors = [ACCENT_AMBER if i == len(cat_counts) - 1 else ACCENT_BLUE
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
                font=dict(family=PLOT_FONT, size=13, color=TEXT_DARK),
                title_font_color=TEXT_DARK,
                hoverlabel=dict(font_color=TEXT_DARK, bgcolor=CARD_BG),
                xaxis=dict(title="Number of Queries", gridcolor=BORDER, zerolinecolor=BORDER),
                yaxis=dict(title=""),
                margin=dict(t=50, b=20, l=10, r=10),
            )
            st.plotly_chart(fig_bar, width="stretch")

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
            width="stretch",
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
            width="stretch",
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
        pdf.set_text_color(26, 58, 92)  # deep blue — print-friendly (white PDF page)
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
# Auto-refresh (same-session rerun so login state survives; disable with
# DASHBOARD_REFRESH_SECS=0 e.g. for automated tests)
# ---------------------------------------------------------------------------

render_dashboard()

REFRESH_SECS = int(os.environ.get("DASHBOARD_REFRESH_SECS", "30"))
if REFRESH_SECS > 0:
    time.sleep(REFRESH_SECS)
    st.rerun()
