"""
Deterministic mandate extraction. No LLM. Rule-based parsing over:
  - mandate_descriptions (from mandates table)
  - firm_strategy / firm_keywords (firms.strategy, firms.apollo_description,
    firms.apollo_keywords, firms.apollo_industries)
  - firm_type (already structured) — used as base evidence

Produces structured columns on firms:
  accepts_debt, accepts_equity, accepts_project_finance, accepts_credit,
  accepts_growth
  extracted_sectors (json)
  extracted_geographies (json)
  extracted_check_min_usd_m, extracted_check_max_usd_m
  mandate_signal_score   (0..1 — how much signal we got)

Idempotent: writes over existing columns each run. Read-only on staging/source files.
"""
from __future__ import annotations

import json
import re
import sqlite3
import yaml
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "db" / "investors.db"
TAXONOMY = ROOT / "taxonomy.yml"


# ---------------------------------------------------------------------
# Rule packs
# ---------------------------------------------------------------------

DEBT_PATTERNS = re.compile(
    r"\b("
    r"debt|senior debt|junior debt|mezzanine|mezz|"
    r"private credit|credit fund|credit strategy|credit firm|"
    r"venture debt|growth debt|revenue based financing|rbf|"
    r"lending|lender|loan|loans|term loan|"
    r"unitranche|first lien|second lien|"
    r"project finance|project-finance|project financing|"
    r"non[- ]dilutive|nondilutive|"
    r"asset[- ]based|asset based|"
    r"subordinated"
    r")\b",
    re.IGNORECASE,
)

EQUITY_PATTERNS = re.compile(
    r"\b("
    r"equity|growth equity|venture capital|venture fund|preferred equity|"
    r"buyout|buyouts|lbo|"
    r"private equity\b(?! firms)|"   # exclude generic "private equity firms"
    r"series [a-d]|seed|pre[- ]seed|"
    r"angel investor|angel investing|"
    r"primary equity|control equity|minority equity"
    r")\b",
    re.IGNORECASE,
)

PROJECT_FINANCE_PATTERNS = re.compile(
    r"\b("
    r"project finance|project[- ]level|project debt|project equity|"
    r"infrastructure (debt|equity)?|"
    r"renewables? infrastructure|"
    r"power[- ]project|"
    r"opex finance|capex finance|"
    r"tax credit financing|tax equity"
    r")\b",
    re.IGNORECASE,
)

CREDIT_PATTERNS = re.compile(
    r"\b(credit|lender finance|tailored credit|structured credit|"
    r"specialty (lender|finance|credit))\b",
    re.IGNORECASE,
)

GROWTH_PATTERNS = re.compile(
    r"\b(growth (capital|equity|debt|financing|stage)|"
    r"expansion (capital|financing|stage)|scale[- ]up)\b",
    re.IGNORECASE,
)

# Geography markers
GEO_RULES = [
    ("us",         re.compile(r"\b(us|usa|united states|u\.s\.|u\.s\.a\.|north america|nyc|new york)\b", re.I)),
    ("canada",     re.compile(r"\b(canada|toronto|vancouver|quebec|montreal)\b", re.I)),
    ("uk",         re.compile(r"\b(uk|united kingdom|england|london|britain|british)\b", re.I)),
    ("germany",    re.compile(r"\bgerman(y|ic)\b|\b(munich|berlin|frankfurt)\b", re.I)),
    ("france",     re.compile(r"\bfrenc?h\b|\bfrance\b|\bparis\b", re.I)),
    ("netherlands",re.compile(r"\bnetherlands\b|\bdutch\b|\bamsterdam\b", re.I)),
    ("switzerland",re.compile(r"\bswiss\b|\bswitzerland\b|\bzurich\b|\bgeneva\b", re.I)),
    ("europe_other", re.compile(r"\beuropean?\b|\beu\b|\b(spain|italy|sweden|norway|denmark|portugal)\b", re.I)),
    ("middle_east", re.compile(r"\b(middle east|uae|saudi|qatar|gcc|dubai|abu dhabi|oman)\b", re.I)),
    ("asia",       re.compile(r"\b(asia|japan|china|singapore|hong kong|korea|india|asean)\b", re.I)),
    ("global",     re.compile(r"\b(global|worldwide|international)\b", re.I)),
]
# Negative US: phrases that *exclude* US (e.g., "OECD ex-US", "Europe-only")
US_NEG = re.compile(r"\b(no (us|emerging markets)|except (the )?us|outside (the )?us|europe only|ex[- ]us)\b", re.I)

# Sector matchers — derive slugs from taxonomy.yml + keyword expansions
SECTOR_MATCHERS = [
    ("solar",          re.compile(r"\b(solar|pv|photovoltaic)\b", re.I)),
    ("wind",           re.compile(r"\b(wind|offshore wind|onshore wind)\b", re.I)),
    ("storage",        re.compile(r"\b(energy storage|bess|battery storage|battery|batteries|lithium[- ]ion)\b", re.I)),
    ("ev_charging",    re.compile(r"\b(ev|ev charging|charging|electric vehicle)\b", re.I)),
    ("hydrogen",       re.compile(r"\bhydrogen\b|\bh2\b", re.I)),
    ("data_centers",   re.compile(r"\b(data centers?|data centres?|digital infra|hyperscalers?|colocation)\b", re.I)),
    ("infrastructure", re.compile(r"\b(infrastructure|infra)\b", re.I)),
    ("renewables",     re.compile(r"\b(renewables?|renewable energy|cleantech|clean tech|clean energy)\b", re.I)),
    ("energy_transition", re.compile(r"\b(energy transition|decarboniz(ation|ed)|climate tech|carbon)\b", re.I)),
    ("agtech",         re.compile(r"\b(ag[- ]?tech|agritech|agriculture)\b", re.I)),
    ("manufacturing",  re.compile(r"\b(manufactur|industrial)\b", re.I)),
    ("fintech",        re.compile(r"\bfintech\b|\bfin[- ]tech\b", re.I)),
    ("ai",             re.compile(r"\b(ai|artificial intelligence|machine learning|ml)\b", re.I)),
    ("biotech",        re.compile(r"\b(biotech|pharma|life sciences)\b", re.I)),
    ("software",       re.compile(r"\b(saas|software|enterprise software)\b", re.I)),
    ("real_estate",    re.compile(r"\b(real estate|cre|commercial real estate)\b", re.I)),
    ("consumer",       re.compile(r"\b(consumer|dtc|d2c|retail)\b", re.I)),
]

# Check-size patterns — context-anchored to avoid picking up AUM / fund size.
# Only extract a number when it's near vocabulary like "ticket", "check",
# "investment size", "writes", "from $X to $Y", "$X-$Y" range.
CHECK_CONTEXT = re.compile(
    r"(?:ticket|tickets|check size|check sizes?|invest(?:ment|ing|s)? size|"
    r"deploy|writes|range from|typical investment|"
    r"investments? rang|sizes? rang|"
    r"loans? rang|loans? typically|loans? from)",
    re.IGNORECASE,
)
CHECK_RE = re.compile(
    r"\$?\s*(\d+(?:\.\d+)?)\s*(m|mm|million|b|billion)?\s*[-–to]+\s*"
    r"\$?\s*(\d+(?:\.\d+)?)\s*(m|mm|million|b|billion)?",
    re.IGNORECASE,
)


def parse_check_sizes(text: str) -> tuple[float | None, float | None]:
    """Pull check size only when adjacent to ticket / check / investment context.
    Caps at $5B because larger numbers are almost always AUM or fund size."""
    if not text:
        return None, None
    # Find context anchors first; within each, scan a 120-char window for ranges
    for ctx in CHECK_CONTEXT.finditer(text):
        start = max(0, ctx.start() - 60)
        end = min(len(text), ctx.end() + 120)
        window = text[start:end]
        for m in CHECK_RE.finditer(window):
            lo = float(m.group(1))
            hi = float(m.group(3))
            if m.group(2) and m.group(2)[0].lower() == "b":
                lo *= 1000
            if m.group(4) and m.group(4)[0].lower() == "b":
                hi *= 1000
            if 0 < lo <= hi <= 5000:  # cap at $5B — anything bigger is AUM
                return lo, hi
    return None, None


# ---------------------------------------------------------------------
# Main extractor
# ---------------------------------------------------------------------

def extract(firm: sqlite3.Row, mandate_text: str | None) -> dict:
    parts = [firm["strategy"], firm["apollo_description"], firm["apollo_keywords"], firm["apollo_industries"]]
    blob = " | ".join(p for p in parts if p) + (" | " + mandate_text if mandate_text else "")
    blob_low = blob.lower()

    accepts_debt = 1 if DEBT_PATTERNS.search(blob) else (1 if firm["type"] == "lender" else None)
    accepts_equity = 1 if EQUITY_PATTERNS.search(blob) else None
    accepts_project_finance = 1 if PROJECT_FINANCE_PATTERNS.search(blob) else None
    accepts_credit = 1 if CREDIT_PATTERNS.search(blob) else None
    accepts_growth = 1 if GROWTH_PATTERNS.search(blob) else None

    # Firm-type defaults
    if firm["type"] == "infra_fund":
        accepts_project_finance = accepts_project_finance or 1
    if firm["type"] in ("lender",):
        accepts_debt = 1
    if firm["type"] in ("vc", "pe_firm", "growth_equity", "family_office", "angel_network"):
        accepts_equity = accepts_equity or 1

    sectors = []
    for slug, pat in SECTOR_MATCHERS:
        if pat.search(blob):
            sectors.append(slug)

    geographies = []
    for slug, pat in GEO_RULES:
        if pat.search(blob):
            geographies.append(slug)
    # Honor explicit exclusions of US
    if "us" in geographies and US_NEG.search(blob):
        geographies.remove("us")

    cmin, cmax = parse_check_sizes(blob)

    # Signal score: how much structured info we extracted
    n_signals = sum(1 for v in (
        accepts_debt, accepts_equity, accepts_project_finance, accepts_credit, accepts_growth
    ) if v) + (1 if sectors else 0) + (1 if geographies else 0) + (1 if cmin or cmax else 0)
    signal_score = round(min(1.0, n_signals / 8.0), 3)

    return {
        "accepts_debt": accepts_debt,
        "accepts_equity": accepts_equity,
        "accepts_project_finance": accepts_project_finance,
        "accepts_credit": accepts_credit,
        "accepts_growth": accepts_growth,
        "extracted_sectors": json.dumps(sorted(set(sectors))),
        "extracted_geographies": json.dumps(sorted(set(geographies))),
        "extracted_check_min_usd_m": cmin,
        "extracted_check_max_usd_m": cmax,
        "mandate_signal_score": signal_score,
    }


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    # Build per-firm aggregated mandate text
    mandate_by_firm: dict[str, str] = {}
    for r in conn.execute(
        "SELECT firm_id, GROUP_CONCAT(description, ' | ') AS desc FROM mandates GROUP BY firm_id"
    ).fetchall():
        mandate_by_firm[r["firm_id"]] = r["desc"]

    rows = conn.execute(
        """SELECT firm_id, name_canonical, type, strategy, apollo_description,
                  apollo_keywords, apollo_industries
           FROM firms"""
    ).fetchall()

    print(f"Extracting structured fields for {len(rows)} firms ...\n")
    updated = 0
    histogram = {"accepts_debt": 0, "accepts_equity": 0, "accepts_project_finance": 0,
                 "accepts_credit": 0, "accepts_growth": 0,
                 "with_sector": 0, "with_geo": 0, "with_check_size": 0}
    for r in rows:
        ex = extract(r, mandate_by_firm.get(r["firm_id"]))
        cols = ", ".join(f"{k}=?" for k in ex.keys())
        vals = list(ex.values()) + [datetime.now(timezone.utc).isoformat(), r["firm_id"]]
        conn.execute(
            f"UPDATE firms SET {cols}, mandate_extracted_at=? WHERE firm_id=?",
            vals,
        )
        updated += 1
        for k in ("accepts_debt", "accepts_equity", "accepts_project_finance", "accepts_credit", "accepts_growth"):
            if ex[k]:
                histogram[k] += 1
        if ex["extracted_sectors"] != "[]":
            histogram["with_sector"] += 1
        if ex["extracted_geographies"] != "[]":
            histogram["with_geo"] += 1
        if ex["extracted_check_min_usd_m"] or ex["extracted_check_max_usd_m"]:
            histogram["with_check_size"] += 1
    conn.commit()

    print(f"Updated {updated} firms.")
    print("\n=== Structured signal coverage ===")
    for k, v in histogram.items():
        print(f"  {k:<26} {v:>4} / {updated}")

    # Sample US-debt-eligible firms
    print("\n=== Sample: US debt-eligible firms (accepts_debt=1, has 'us' or 'global' geo) ===")
    rows = conn.execute("""
        SELECT name_canonical, type, mandate_signal_score,
               extracted_sectors, extracted_geographies,
               extracted_check_min_usd_m, extracted_check_max_usd_m
        FROM firms
        WHERE accepts_debt = 1
          AND (extracted_geographies LIKE '%us%' OR extracted_geographies LIKE '%global%')
        ORDER BY mandate_signal_score DESC, name_canonical
        LIMIT 12
    """).fetchall()
    for r in rows:
        print(f"  {r['name_canonical'][:35]:<35} {r['type'] or '-':<14} "
              f"score={r['mandate_signal_score']:.2f}  "
              f"sectors={r['extracted_sectors'][:35]:<35}  "
              f"geo={r['extracted_geographies'][:25]:<25} "
              f"${r['extracted_check_min_usd_m']}-{r['extracted_check_max_usd_m']}")
    conn.close()


if __name__ == "__main__":
    main()
