"""
SDP Investor Finder — Streamlit MVP.

Upload a pitch deck, memo, or paste a transcript. The app uses Haiku to
extract structured deal criteria, then runs the hardened scorer against
the investor network DB. Returns the top 5 (configurable) investors with
contact, score-breakdown, and ready-to-copy email.

Run locally:
    streamlit run app.py
Deploy:
    Push to GitHub, link the repo at https://share.streamlit.io, set
    secrets {ANTHROPIC_API_KEY, APP_PASSWORD} in the dashboard.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pandas as pd
import streamlit as st
import yaml

# --- Make our scripts importable ---
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "scripts"))

# Trigger .env load + DB path
from _lib import DB, connect  # noqa: E402
from _score import ScoringCriteria, score_candidates  # noqa: E402


# The deal extractor module has a numeric prefix that breaks normal import;
# load it explicitly. Register in sys.modules BEFORE exec so pydantic's
# forward-reference resolver can find the module globals.
spec = importlib.util.spec_from_file_location("deal_extract", ROOT / "scripts" / "12_deal_extract.py")
deal_extract = importlib.util.module_from_spec(spec)
sys.modules["deal_extract"] = deal_extract
spec.loader.exec_module(deal_extract)


# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------
st.set_page_config(
    page_title="SDP Investor Finder",
    page_icon="🎯",
    layout="wide",
)

# Brand-ish CSS
st.markdown("""
<style>
  .stApp { background: #fafbfc; }
  h1, h2, h3 { color: #0B2545; }
  .candidate-card { background: white; border: 1px solid #e1e8f0; border-radius: 8px;
                    padding: 16px; margin-bottom: 12px; }
  .badge { display: inline-block; padding: 3px 8px; border-radius: 12px;
           font-size: 12px; margin-right: 4px; }
  .badge-firm { background: #0B2545; color: white; }
  .badge-attio { background: #16A34A; color: white; }
  .badge-attio-weak { background: #D97706; color: white; }
  .badge-engagement { background: #EAB308; color: black; }
  .score { font-size: 28px; font-weight: 700; color: #0B2545; }
  .why { color: #64748b; font-size: 12px; font-family: monospace; }
</style>
""", unsafe_allow_html=True)


# ----------------------------------------------------------------------
# Auth (simple shared password)
# ----------------------------------------------------------------------
def check_password():
    """Single-password gate. Set APP_PASSWORD via st.secrets or env var.
    If neither is set, the app runs unprotected (dev mode)."""
    expected = (st.secrets.get("APP_PASSWORD") if hasattr(st, "secrets") and "APP_PASSWORD" in st.secrets
                else os.environ.get("APP_PASSWORD"))
    if not expected:
        return True  # no password configured, open mode

    if st.session_state.get("authed"):
        return True

    pw = st.text_input("Password", type="password", key="pw_input")
    if st.button("Sign in"):
        if pw == expected:
            st.session_state["authed"] = True
            st.rerun()
        else:
            st.error("Incorrect password")
    return False


# ----------------------------------------------------------------------
# Load taxonomy for dropdowns
# ----------------------------------------------------------------------
@st.cache_data
def load_taxonomy():
    with open(ROOT / "taxonomy.yml") as f:
        tax = yaml.safe_load(f)
    return {
        "sectors": sorted(tax["sector"].keys()),
        "geographies": sorted(tax["geography"].keys()),
        "stages": sorted(tax["stage_focus"].keys()),
    }


TAX = load_taxonomy()


# ----------------------------------------------------------------------
# DB stats panel
# ----------------------------------------------------------------------
@st.cache_data(ttl=60)
def db_stats():
    conn = connect()
    return {
        "firms": conn.execute("SELECT COUNT(*) FROM firms").fetchone()[0],
        "contacts": conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0],
        "engagements": conn.execute("SELECT COUNT(*) FROM engagements").fetchone()[0],
        "with_attio": conn.execute("SELECT COUNT(*) FROM firms WHERE attio_id IS NOT NULL").fetchone()[0],
        "debt_capable": conn.execute("SELECT COUNT(*) FROM firms WHERE accepts_debt = 1 OR type IN ('lender','infra_fund')").fetchone()[0],
        "with_sector_tag": conn.execute("SELECT COUNT(*) FROM firms WHERE extracted_sectors != '[]'").fetchone()[0],
    }


# ----------------------------------------------------------------------
# Render a single candidate card
# ----------------------------------------------------------------------
def render_candidate(c: dict, rank: int):
    firm_badge = f'<span class="badge badge-firm">{c["firm_type"] or "—"}</span>'
    attio_badge = ""
    if c["attio_connection_strength"]:
        css = "badge-attio" if c["attio_connection_strength"] in ("Good", "Strong", "Very strong") else "badge-attio-weak"
        attio_badge = f'<span class="badge {css}">{c["attio_connection_strength"]}</span>'
    eng_badge = ""
    if c["last_engagement_status"]:
        eng_badge = f'<span class="badge badge-engagement">{c["last_engagement_status"]}</span>'

    col_left, col_right = st.columns([5, 1])
    with col_left:
        st.markdown(
            f"### #{rank}  {c['firm']}  "
            f"{firm_badge} {attio_badge} {eng_badge}",
            unsafe_allow_html=True,
        )
        if c["contact_name"]:
            owner = f" · owned by **{c['relationship_owner']}**" if c["relationship_owner"] else ""
            title = f" — {c['contact_title']}" if c["contact_title"] else ""
            st.markdown(f"**{c['contact_name']}**{title}{owner}")
            if c["contact_email"]:
                st.code(c["contact_email"], language=None)
        else:
            st.markdown("_(no contact on file — firm-only)_")

        if c["last_sdp_client"] or c["last_feedback"]:
            with st.expander("📨 Last touch"):
                if c["last_sdp_client"]:
                    st.markdown(f"**Pitched for:** {c['last_sdp_client']}  ·  status: `{c['last_engagement_status']}`")
                if c["last_engagement_date"]:
                    st.markdown(f"**Date:** {c['last_engagement_date']}")
                if c["last_feedback"]:
                    st.markdown(f"**Feedback:** {c['last_feedback']}")

        if c["mandate_descriptions"]:
            with st.expander("📋 Mandate on file"):
                st.markdown(c["mandate_descriptions"])
        if c["firm_strategy"]:
            with st.expander("🏢 Firm strategy"):
                st.markdown(c["firm_strategy"][:800] + ("…" if len(c["firm_strategy"] or "") > 800 else ""))

        st.markdown(
            f'<div class="why">Sectors: {", ".join(c["extracted_sectors"]) or "—"} · '
            f'Geo: {", ".join(c["extracted_geographies"]) or "—"} · '
            f'Score breakdown: {" | ".join(c["why"])}</div>',
            unsafe_allow_html=True,
        )

    with col_right:
        st.markdown(f'<div class="score">{c["score"]}</div>', unsafe_allow_html=True)
        if c["url"]:
            st.markdown(f"[website ↗]({c['url']})")
        if c["firm_linkedin"]:
            st.markdown(f"[LinkedIn ↗]({c['firm_linkedin']})")

    st.divider()


# ----------------------------------------------------------------------
# Main app
# ----------------------------------------------------------------------
if not check_password():
    st.stop()

st.title("🎯 SDP Investor Finder")
st.caption("Upload a deck, transcript, or memo. Get the top-matched investors from your network.")

# Sidebar — DB stats + tweakable params
with st.sidebar:
    st.subheader("Network coverage")
    s = db_stats()
    st.metric("Firms", s["firms"])
    st.metric("Contacts", s["contacts"])
    st.metric("Engagements", s["engagements"])
    st.metric("Debt-capable firms", s["debt_capable"])
    st.metric("With sector tag", f"{s['with_sector_tag']} / {s['firms']}")
    st.metric("With Attio data", s["with_attio"])
    st.divider()
    top_n = st.slider("Top N results", min_value=3, max_value=20, value=5)
    include_passed = st.checkbox("Include firms that previously passed", value=False)
    st.caption(f"DB: `{DB.name}` · Refreshed each query.")


# ----------------------------------------------------------------------
# Step 1: Upload
# ----------------------------------------------------------------------
st.subheader("1. Provide the deal")

tab1, tab2 = st.tabs(["📄 Upload file", "📝 Paste text"])

deck_text = None
deck_label = None

with tab1:
    uploaded = st.file_uploader(
        "PDF deck, Word doc (.docx), or plain text",
        type=["pdf", "docx", "txt", "md"],
        help="Will extract text and run it through Claude to detect deal criteria.",
    )
    if uploaded is not None:
        try:
            deck_text = deal_extract.extract_text_from_upload(uploaded.name, uploaded.getvalue())
            deck_label = uploaded.name
            st.success(f"Extracted {len(deck_text):,} characters from {uploaded.name}")
            with st.expander("Preview extracted text"):
                st.text(deck_text[:3000] + ("…" if len(deck_text) > 3000 else ""))
        except Exception as e:
            st.error(f"Could not read file: {e}")

with tab2:
    pasted = st.text_area(
        "Paste a transcript, memo, or any text describing the deal",
        height=200,
        placeholder="E.g. 'Project Aurora — 100 MW utility-scale solar in West Texas, "
                    "seeking $80m senior project debt for construction…'",
    )
    if pasted.strip():
        deck_text = pasted
        deck_label = "pasted text"

# ----------------------------------------------------------------------
# Step 2: Extract criteria
# ----------------------------------------------------------------------
if deck_text:
    st.subheader("2. Detected deal criteria")

    if "criteria" not in st.session_state or st.session_state.get("last_text_hash") != hash(deck_text):
        with st.spinner("Reading the deal with Claude…"):
            try:
                criteria = deal_extract.extract_deal_criteria(deck_text)
                st.session_state["criteria"] = criteria.model_dump()
                st.session_state["last_text_hash"] = hash(deck_text)
            except Exception as e:
                st.error(f"Extraction failed: {e}")
                st.stop()

    c = st.session_state["criteria"]
    if c["company_name"]:
        st.markdown(f"**Company:** {c['company_name']}")
    st.info(c["one_line_summary"])

    # Editable form — user can override before scoring
    col1, col2, col3 = st.columns(3)
    with col1:
        capital_type = st.selectbox(
            "Capital type",
            options=["debt", "equity", "both", "unknown"],
            index=["debt", "equity", "both", "unknown"].index(c["capital_type"]),
            help="Detected from the deal. Override if needed.",
        )
    with col2:
        primary_sector = st.selectbox(
            "Primary sector",
            options=TAX["sectors"],
            index=TAX["sectors"].index(c["primary_sector"]) if c["primary_sector"] in TAX["sectors"] else TAX["sectors"].index("other"),
        )
    with col3:
        geographies = st.multiselect(
            "Geographies",
            options=TAX["geographies"],
            default=[g for g in c.get("geographies", []) if g in TAX["geographies"]],
        )

    secondary = st.multiselect(
        "Related sectors (optional)",
        options=TAX["sectors"],
        default=[s for s in c.get("secondary_sectors", []) if s in TAX["sectors"]],
    )

    col4, col5 = st.columns(2)
    with col4:
        check_min = st.number_input(
            "Check size min ($M)", min_value=0.0, step=1.0,
            value=float(c.get("check_size_min_usd_m") or 0.0),
        )
    with col5:
        check_max = st.number_input(
            "Check size max ($M)", min_value=0.0, step=1.0,
            value=float(c.get("check_size_max_usd_m") or 0.0),
        )

    extra_keywords = st.text_input(
        "Extra keywords (comma-separated, optional)",
        value=", ".join(c.get("key_terms", [])),
        help="Phrases lifted from the deal that should match investor mandates.",
    )

    # ----------------------------------------------------------------------
    # Step 3: Run scorer
    # ----------------------------------------------------------------------
    st.subheader("3. Top matches from your network")

    if st.button("Find investors", type="primary", use_container_width=True):
        with st.spinner("Scoring the network…"):
            conn = connect()
            criteria_obj = ScoringCriteria(
                require_debt=(capital_type in ("debt", "both")),
                require_equity=(capital_type in ("equity", "both")),
                primary_sector=primary_sector if primary_sector != "other" else None,
                secondary_sectors=secondary,
                geographies=geographies,
                include_passed=include_passed,
                extra_keywords=[k.strip() for k in extra_keywords.split(",") if k.strip()],
            )
            cands, rejected = score_candidates(conn, criteria_obj)
            conn.close()

        st.caption(
            f"Scanned {sum(rejected.values()) + len(cands)} firm×contact rows · "
            f"Rejected: {dict(rejected)} · Surviving candidates: **{len(cands)}**"
        )

        for i, cand in enumerate(cands[:top_n], 1):
            render_candidate(cand, i)

        if len(cands) > top_n:
            with st.expander(f"… and {len(cands) - top_n} more"):
                df = pd.DataFrame(cands[top_n:])
                cols = ["score", "firm", "firm_type", "contact_name", "contact_email",
                        "relationship_owner", "attio_connection_strength",
                        "last_engagement_status", "last_sdp_client"]
                st.dataframe(df[cols], use_container_width=True)

        if cands:
            # Download CSV
            export_df = pd.DataFrame(cands)
            export_cols = ["score", "firm", "firm_type", "contact_name", "contact_email",
                           "contact_title", "relationship_owner", "attio_connection_strength",
                           "attio_last_interaction", "last_engagement_status", "last_sdp_client",
                           "last_feedback", "mandate_descriptions", "extracted_sectors",
                           "extracted_geographies", "url", "firm_linkedin"]
            export_df = export_df[[c for c in export_cols if c in export_df.columns]]
            ts = time.strftime("%Y%m%d-%H%M%S")
            label = (deck_label or "deal").replace(" ", "_").replace("/", "_")[:40]
            st.download_button(
                "📥 Download all candidates as CSV",
                export_df.to_csv(index=False).encode("utf-8"),
                file_name=f"sdp_matches_{label}_{ts}.csv",
                mime="text/csv",
            )
else:
    st.caption("⬆️ Upload a file or paste text to start.")
