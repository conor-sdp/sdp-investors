"""
Ingest a PitchBook investor CSV export into firms.pb_* and contacts.

For each row:
  - Match firm by domain → exact normalized name → fuzzy name (>=90).
  - Write all pb_* columns; mark provenance.
  - Update derived structured fields where the PB data is more authoritative:
      * type (if currently NULL — never overwrite curated values)
      * accepts_debt / accepts_equity / accepts_project_finance — set 1 when
        PB's `preferred_investment_types` clearly says so.
      * extracted_check_min/max from PB's preferred_investment_amount.
  - If the row has a Primary Contact + email, upsert into contacts.
  - If no match in our firm DB, create a new firm (provenance='pitchbook_only').

Idempotent: re-running overwrites pb_* with the latest CSV; existing non-PB
columns are preserved.

Usage:
    .venv/bin/python scripts/13_pitchbook_ingest.py [path/to/file.csv]
Default CSV path: ~/Downloads/Investors SDP pb enriched.csv
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from rapidfuzz import fuzz, process

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import (  # noqa: E402
    DB, connect, norm_firm_name, extract_domain, new_ulid, write_source,
    norm_email, parse_location,
)

DEFAULT_CSV = Path.home() / "Downloads" / "Investors SDP pb enriched.csv"

# --- Column mapping (CSV → conceptual field) ------------------------
CSV_MAP = {
    "name_primary":    "ellipsis 2",
    "name_secondary":  "ellipsis",
    "investments":     "Invesetments",
    "active_portfolio":"ACtive portfolio",
    "aum":             "AUM",
    "investor_type":   "Primary Investor Type",
    "hq_location":     "HQ location",
    "description":     "Description",
    "industries":      "PReferred industry",
    "investment_types":"Preferred investment types",
    "website":         "Website",
    "url":             "URL",
    "contact_name":    "Primary Contact",
    "contact_title":   "Title",
    "contact_phone":   "ellipsis 10",
    "check_max":       "Preferred investment amount Max",
    "check_min":       "Preferred investment amount",
    "contact_email":   "ellipsis 12",
}

# --- PB investor type → our firm_type taxonomy slug ----------------
PB_TYPE_TO_SLUG = {
    "Venture Capital":              "vc",
    "PE/Buyout":                    "pe_firm",
    "Private Equity":               "pe_firm",
    "Asset Manager":                "other",
    "Family Office":                "family_office",
    "Corporate Venture Capital":    "corporate_vc",
    "Corporation":                  "strategic",
    "Infrastructure":               "infra_fund",
    "Impact Investing":             "vc",
    "Limited Partner":              "other",
    "Growth/Expansion":             "growth_equity",
    "Lender/Debt Provider":         "lender",
    "Real Estate":                  "other",
    "Holding Company":              "strategic",
    "PE-Backed Company":            "other",
    "Angel Group":                  "angel_network",
    "VC-Backed Company":            "other",
    "Other":                        "other",
}

# --- Investment-type tokens → accepts_* flags ----------------------
DEBT_TOKENS = {
    "debt", "mezzanine", "loan", "credit line", "secured debt",
    "convertible debt", "debt refinancing", "revolving credit",
    "exit financing", "acquisition financing",
}
EQUITY_TOKENS = {
    "buyout", "pe growth", "early stage vc", "later stage vc", "seed",
    "pre-seed", "series", "growth equity", "accelerator", "incubator",
    "grant", "merger", "add-on",
}
PROJECT_FINANCE_TOKENS = {"project finance", "project debt", "project equity"}
CREDIT_TOKENS = {"debt", "credit", "mezzanine"}
GROWTH_TOKENS = {"pe growth", "growth equity", "later stage vc"}


# --- Parsers --------------------------------------------------------

def parse_float(v) -> float | None:
    """'1,500.00' -> 1500.0; blanks -> None."""
    if pd.isna(v):
        return None
    s = str(v).strip().replace(",", "")
    if not s or s.lower() in ("nan", "n/a"):
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def parse_int(v) -> int | None:
    f = parse_float(v)
    return int(f) if f is not None else None


def parse_investment_types(s) -> dict:
    """Map PB's preferred_investment_types text to our boolean signals."""
    if pd.isna(s):
        return {}
    text = str(s).lower()
    return {
        "accepts_debt":            1 if any(t in text for t in DEBT_TOKENS) else None,
        "accepts_equity":          1 if any(t in text for t in EQUITY_TOKENS) else None,
        "accepts_project_finance": 1 if any(t in text for t in PROJECT_FINANCE_TOKENS) else None,
        "accepts_credit":          1 if any(t in text for t in CREDIT_TOKENS) else None,
        "accepts_growth":          1 if any(t in text for t in GROWTH_TOKENS) else None,
    }


def parse_hq(raw: str | None):
    """'Princeton, NJ' -> (city='Princeton', country='us', state='NJ')."""
    if not raw or (isinstance(raw, float) and pd.isna(raw)):
        return None, None, None
    s = str(raw).strip()
    parts = re.split(r"\s*,\s*", s, maxsplit=1)
    if len(parts) == 2:
        city, region = parts[0], parts[1]
        if re.fullmatch(r"[A-Z]{2}", region):
            return city, "us", region
        return city, None, region
    return s, None, None


# --- Firm matching --------------------------------------------------

def match_firm(conn, name: str, domain: str | None) -> tuple[str | None, str]:
    """Returns (firm_id_or_None, method). method ∈ {domain,name,fuzzy,none}.

    Fuzzy matching uses token_sort_ratio (not WRatio) because WRatio's
    partial-substring scoring over-rewards short strings (e.g. 'vistara'
    matching 'ara' at >90 because of the substring overlap). Threshold
    raised to 92 and we also require the shorter-token string to be at
    least 6 chars to avoid false-positives on short normalized names.
    """
    if domain:
        row = conn.execute(
            "SELECT firm_id FROM firms WHERE lower(domain) = ?",
            (domain.lower(),),
        ).fetchone()
        if row:
            return row[0], "domain"
    if not name:
        return None, "none"
    nn = norm_firm_name(name)
    if nn:
        row = conn.execute(
            "SELECT firm_id, name_canonical FROM firms WHERE lower(name_canonical) = ?",
            (name.lower(),),
        ).fetchone()
        if row:
            return row[0], "name"
        all_firms = conn.execute("SELECT firm_id, name_canonical FROM firms").fetchall()
        norm_to_id = {norm_firm_name(f[1]): f[0] for f in all_firms}
        if nn in norm_to_id:
            return norm_to_id[nn], "name"
        # Fuzzy — strict to avoid 'vistara'/'ara' style traps
        if len(nn) >= 6:
            m = process.extractOne(nn, list(norm_to_id.keys()),
                                   scorer=fuzz.token_sort_ratio)
            if m and m[1] >= 92 and len(m[0]) >= 6:
                return norm_to_id[m[0]], "fuzzy"
    return None, "none"


def reset_prior_pb_data(conn) -> dict:
    """Wipe everything the prior PB ingest wrote, so a re-run is clean.
    Returns counts of what was removed."""
    counts = {}
    # Find PB-added contacts via sources table
    pb_contact_ids = [r[0] for r in conn.execute(
        """SELECT DISTINCT entity_id FROM sources
           WHERE entity_type='contact' AND source_file LIKE '%pb enriched%'"""
    ).fetchall()]
    if pb_contact_ids:
        conn.execute(
            f"DELETE FROM contacts WHERE contact_id IN ({','.join('?' * len(pb_contact_ids))})",
            pb_contact_ids,
        )
        counts["contacts_deleted"] = len(pb_contact_ids)
    # Delete sources rows from prior PB ingest
    deleted_sources = conn.execute(
        "DELETE FROM sources WHERE source_file LIKE '%pb enriched%'"
    ).rowcount
    counts["source_rows_deleted"] = deleted_sources
    # Delete pitchbook_only firms (entire row — they were created from PB only)
    pb_only_firm_ids = [r[0] for r in conn.execute(
        "SELECT firm_id FROM firms WHERE pb_provenance='pitchbook_only'"
    ).fetchall()]
    if pb_only_firm_ids:
        conn.execute(
            f"DELETE FROM firms WHERE firm_id IN ({','.join('?' * len(pb_only_firm_ids))})",
            pb_only_firm_ids,
        )
        counts["pb_only_firms_deleted"] = len(pb_only_firm_ids)
    # Reset pb_* columns on remaining firms
    conn.execute("""
        UPDATE firms SET
            pb_name=NULL, pb_description=NULL, pb_aum_usd_m=NULL,
            pb_total_investments=NULL, pb_active_portfolio=NULL,
            pb_investor_type=NULL, pb_hq_location=NULL,
            pb_preferred_industries=NULL, pb_preferred_investment_types=NULL,
            pb_website=NULL, pb_primary_contact_name=NULL,
            pb_primary_contact_title=NULL, pb_primary_contact_email=NULL,
            pb_primary_contact_phone=NULL, pb_check_size_min_usd_m=NULL,
            pb_check_size_max_usd_m=NULL, pb_provenance=NULL,
            pb_ingested_at=NULL
        WHERE pb_provenance IS NOT NULL""")
    counts["firms_pb_fields_reset"] = conn.execute(
        "SELECT changes()").fetchone()[0]
    return counts


# --- Contact upsert --------------------------------------------------

def upsert_contact(conn, firm_id: str, name: str, email: str | None,
                   title: str | None, phone: str | None) -> str | None:
    """Insert or update a contact. Returns contact_id."""
    if not name and not email:
        return None
    # First check for existing contact by email
    if email:
        existing = conn.execute(
            "SELECT contact_id FROM contacts WHERE lower(email) = ?",
            (email.lower(),),
        ).fetchone()
        if existing:
            cid = existing[0]
            conn.execute(
                """UPDATE contacts SET
                     title = COALESCE(title, ?),
                     phone = COALESCE(phone, ?),
                     full_name = COALESCE(full_name, ?)
                   WHERE contact_id = ?""",
                (title, phone, name, cid),
            )
            return cid
    # Check by name + firm
    if name:
        existing = conn.execute(
            "SELECT contact_id FROM contacts WHERE firm_id = ? AND lower(full_name) = ?",
            (firm_id, name.lower()),
        ).fetchone()
        if existing:
            cid = existing[0]
            conn.execute(
                """UPDATE contacts SET
                     email = COALESCE(email, ?),
                     title = COALESCE(title, ?),
                     phone = COALESCE(phone, ?)
                   WHERE contact_id = ?""",
                (email, title, phone, cid),
            )
            return cid
    # Create new
    parts = (name or "").split(None, 1)
    first = parts[0] if parts else None
    last = parts[1] if len(parts) > 1 else None
    cid = new_ulid()
    conn.execute(
        """INSERT INTO contacts
             (contact_id, firm_id, first_name, last_name, full_name,
              title, email, phone)
           VALUES (?,?,?,?,?,?,?,?)""",
        (cid, firm_id, first, last, name or None, title, email, phone),
    )
    return cid


# --- New firm creation -----------------------------------------------

def create_firm_from_pb(conn, row, domain: str | None) -> str:
    """Create a new firm record from a PitchBook row (no existing match)."""
    fid = new_ulid()
    name = row.get("name_primary") or row.get("name_secondary") or "Unknown"
    norm = norm_firm_name(name)
    type_slug = PB_TYPE_TO_SLUG.get(row.get("investor_type") or "", "other")
    aum = parse_float(row.get("aum"))
    hq_city, hq_country, hq_state = parse_hq(row.get("hq_location"))
    url = row.get("website")
    if isinstance(url, str) and url and not url.startswith("http"):
        url = "https://" + url
    conn.execute(
        """INSERT INTO firms
             (firm_id, name_canonical, name_aliases, domain, url, type,
              aum_usd_m, hq_city, hq_country, strategy,
              sectors, stages, geographies,
              enrichment_status)
           VALUES (?,?,?,?,?,?, ?,?,?,?, ?,?,?, ?)""",
        (fid, name, json.dumps([]), domain, url, type_slug,
         aum, hq_city, hq_country, row.get("description"),
         json.dumps([]), json.dumps([]), json.dumps([]),
         "complete"),
    )
    return fid


# --- Main ------------------------------------------------------------

def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    csv_path = Path(args[0]) if args else DEFAULT_CSV
    if not csv_path.exists():
        sys.exit(f"CSV not found: {csv_path}")
    print(f"Reading {csv_path}")
    if "--reset" in flags:
        print("Resetting prior PB ingest state ...")
        conn0 = connect()
        reset_stats = reset_prior_pb_data(conn0)
        conn0.commit()
        conn0.close()
        for k, v in reset_stats.items():
            print(f"  {k:<25} {v}")
        print()
    raw = pd.read_csv(csv_path)

    # Normalize column access
    def col(row, key):
        cn = CSV_MAP.get(key)
        if cn is None or cn not in row:
            return None
        v = row[cn]
        if pd.isna(v):
            return None
        return v

    conn = connect()

    stats = {"domain": 0, "name": 0, "fuzzy": 0, "new": 0,
             "contacts_added": 0, "contacts_updated": 0,
             "rows_skipped": 0}
    now_iso = datetime.now(timezone.utc).isoformat()

    for _, row in raw.iterrows():
        name = col(row, "name_primary") or col(row, "name_secondary")
        if not name:
            stats["rows_skipped"] += 1
            continue
        domain_raw = col(row, "website") or col(row, "url")
        domain = extract_domain(domain_raw) if domain_raw else None

        firm_id, method = match_firm(conn, str(name), domain)
        if not firm_id:
            firm_id = create_firm_from_pb(conn, {
                "name_primary": col(row, "name_primary"),
                "name_secondary": col(row, "name_secondary"),
                "investor_type": col(row, "investor_type"),
                "hq_location": col(row, "hq_location"),
                "aum": col(row, "aum"),
                "website": col(row, "website"),
                "description": col(row, "description"),
            }, domain)
            method = "new"
        stats[method] += 1

        invest_signals = parse_investment_types(col(row, "investment_types"))
        check_min = parse_float(col(row, "check_min"))
        check_max = parse_float(col(row, "check_max"))
        pb_type = col(row, "investor_type")
        firm_type_slug = PB_TYPE_TO_SLUG.get(pb_type or "")

        # Update firms.pb_* + structured derivations (COALESCE keeps curated data)
        conn.execute(
            """UPDATE firms SET
                  pb_name                       = ?,
                  pb_description                = ?,
                  pb_aum_usd_m                  = ?,
                  pb_total_investments          = ?,
                  pb_active_portfolio           = ?,
                  pb_investor_type              = ?,
                  pb_hq_location                = ?,
                  pb_preferred_industries       = ?,
                  pb_preferred_investment_types = ?,
                  pb_website                    = ?,
                  pb_primary_contact_name       = ?,
                  pb_primary_contact_title      = ?,
                  pb_primary_contact_email      = ?,
                  pb_primary_contact_phone      = ?,
                  pb_check_size_min_usd_m       = ?,
                  pb_check_size_max_usd_m       = ?,
                  pb_provenance                 = ?,
                  pb_ingested_at                = ?,
                  -- Derived merges: only fill where NULL today
                  type             = COALESCE(type, ?),
                  aum_usd_m        = COALESCE(aum_usd_m, ?),
                  accepts_debt     = COALESCE(accepts_debt, ?),
                  accepts_equity   = COALESCE(accepts_equity, ?),
                  accepts_project_finance = COALESCE(accepts_project_finance, ?),
                  accepts_credit   = COALESCE(accepts_credit, ?),
                  accepts_growth   = COALESCE(accepts_growth, ?),
                  extracted_check_min_usd_m = COALESCE(extracted_check_min_usd_m, ?),
                  extracted_check_max_usd_m = COALESCE(extracted_check_max_usd_m, ?)
                WHERE firm_id = ?""",
            (
                col(row, "name_primary") or col(row, "name_secondary"),
                col(row, "description"),
                parse_float(col(row, "aum")),
                parse_int(col(row, "investments")),
                parse_int(col(row, "active_portfolio")),
                pb_type,
                col(row, "hq_location"),
                col(row, "industries"),
                col(row, "investment_types"),
                col(row, "website"),
                col(row, "contact_name"),
                col(row, "contact_title"),
                norm_email(col(row, "contact_email")),
                col(row, "contact_phone"),
                check_min,
                check_max,
                "pitchbook_only" if method == "new" else "enrichment",
                now_iso,
                # derived merges
                firm_type_slug,
                parse_float(col(row, "aum")),
                invest_signals.get("accepts_debt"),
                invest_signals.get("accepts_equity"),
                invest_signals.get("accepts_project_finance"),
                invest_signals.get("accepts_credit"),
                invest_signals.get("accepts_growth"),
                check_min,
                check_max,
                firm_id,
            ),
        )

        # Upsert primary contact
        contact_name = col(row, "contact_name")
        contact_email = norm_email(col(row, "contact_email"))
        if contact_name or contact_email:
            existed_before = bool(contact_email and conn.execute(
                "SELECT 1 FROM contacts WHERE lower(email)=?",
                (contact_email.lower(),)).fetchone())
            cid = upsert_contact(
                conn, firm_id,
                str(contact_name) if contact_name else None,
                contact_email,
                str(col(row, "contact_title") or "") or None,
                str(col(row, "contact_phone") or "") or None,
            )
            if cid:
                if existed_before:
                    stats["contacts_updated"] += 1
                else:
                    stats["contacts_added"] += 1
                # Provenance row
                source_file = csv_path.name
                write_source(conn, source_file, None, int(_) + 2, "contact", cid,
                             {"pb_contact": contact_name, "email": contact_email,
                              "title": col(row, "contact_title")})
        # Provenance for firm enrichment too
        source_file = csv_path.name
        write_source(conn, source_file, None, int(_) + 2, "firm", firm_id,
                     {"pb_name": col(row, "name_primary"),
                      "pb_type": pb_type,
                      "method": method})

    conn.commit()
    print("\n=== PitchBook ingest summary ===")
    for k, v in stats.items():
        print(f"  {k:<22} {v}")

    # Coverage report
    print("\n=== Post-ingest field coverage ===")
    for q in (
        ("firms.pb_description IS NOT NULL", "pb_description"),
        ("firms.pb_aum_usd_m IS NOT NULL", "pb_aum"),
        ("firms.pb_primary_contact_email IS NOT NULL", "pb_email"),
        ("firms.accepts_debt = 1", "accepts_debt (any source)"),
        ("firms.accepts_equity = 1", "accepts_equity (any source)"),
        ("firms.accepts_project_finance = 1", "accepts_project_finance"),
        ("firms.aum_usd_m IS NOT NULL", "aum populated"),
    ):
        n = conn.execute(f"SELECT COUNT(*) FROM firms WHERE {q[0]}").fetchone()[0]
        print(f"  {q[1]:<35} {n} / 333")
    conn.close()


if __name__ == "__main__":
    main()
