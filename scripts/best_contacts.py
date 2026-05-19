"""
Hardened "best contacts for a deal" scorer.

Usage:
    .venv/bin/python scripts/best_contacts.py --debt --sector solar --geo us --top 5
    .venv/bin/python scripts/best_contacts.py --equity --sector data_centers --top 10

Scoring model:
  HARD FILTERS — eliminate firms outright if they fail any:
    --debt    : firms must accept debt (accepts_debt=1 OR firm_type IN lender,infra_fund)
    --equity  : firms must accept equity (accepts_equity=1 OR firm_type IN vc,pe_firm,
                growth_equity,family_office,angel_network)
    --sector  : firm must list the slug in extracted_sectors OR firm_sectors
    --geo us  : firm must list 'us' in extracted_geographies, OR apollo_hq_country='United States',
                OR have NO explicit non-US geo set (i.e. global+ambiguous OK)

  SOFT RANK (additive):
    sector match strength      (sector slug exact match     : +4)
                                (related sector match       : +2 each, cap 2)
    debt-quality signals       (accepts_project_finance     : +3)
                                (accepts_credit              : +2)
                                (mandate text mentions      : +2)
    geography                  (extracted_geographies has us : +3)
                                (apollo_hq_country=USA       : +2)
    relationship warmth        (attio Very strong            : +5)
                                (attio Strong                : +4)
                                (attio Good                  : +3)
                                (attio Weak                  : +1)
                                (last_interaction within 90d : +2)
                                (last_interaction within 1y  : +1)
    prior engagement state     (deck_sent/meeting/inquiry    : +3)
                                (followup                    : +2)
                                (passed                      : -5)
                                (no_response                 : -1)
    fit + contact              (has contact_email            : +1)
                                (mandate_signal_score        : +score*2)

Output: top N firms with reasoning trace.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "db" / "investors.db"

# Related-sector neighborhoods (boost partial matches)
RELATED_SECTORS = {
    "solar":              {"renewables", "energy_transition", "storage", "infrastructure"},
    "wind":               {"renewables", "energy_transition", "infrastructure"},
    "storage":            {"renewables", "energy_transition", "ev_charging", "infrastructure"},
    "ev_charging":        {"renewables", "energy_transition", "storage"},
    "hydrogen":           {"renewables", "energy_transition", "infrastructure"},
    "data_centers":       {"infrastructure", "energy_transition"},
    "infrastructure":     {"renewables", "energy_transition"},
    "renewables":         {"solar", "wind", "storage", "infrastructure", "energy_transition"},
    "energy_transition":  {"solar", "wind", "storage", "renewables", "infrastructure", "hydrogen"},
}

# Sectors → keywords to grep in mandate text for soft match
SECTOR_KEYWORDS = {
    "solar":         ["solar", "pv", "photovoltaic"],
    "wind":          ["wind"],
    "storage":       ["storage", "battery", "bess"],
    "ev_charging":   ["ev", "charging", "electric vehicle"],
    "hydrogen":      ["hydrogen", "h2"],
    "data_centers":  ["data center", "data centre", "digital infra"],
    "infrastructure":["infrastructure", "infra"],
    "renewables":    ["renewable", "cleantech", "clean tech"],
    "energy_transition": ["energy transition", "decarboniz", "climate"],
}


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--debt", action="store_true", help="Require firm accepts debt")
    p.add_argument("--equity", action="store_true", help="Require firm accepts equity")
    p.add_argument("--sector", type=str, default=None,
                   help="Required sector slug (e.g. solar, data_centers, infrastructure)")
    p.add_argument("--geo", type=str, default=None,
                   help="Required geography slug (e.g. us, canada, global)")
    p.add_argument("--top", type=int, default=5)
    p.add_argument("--show", type=int, default=15,
                   help="How many candidates to score+show internally (top-N is sliced from this)")
    p.add_argument("--include-passed", action="store_true",
                   help="Include firms whose latest engagement was 'passed'")
    return p.parse_args()


def jsonl(s):
    if not s:
        return []
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return []


def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def main():
    args = parse_args()
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    sql = "SELECT * FROM v_relationships"
    rows = conn.execute(sql).fetchall()

    DEBT_FIRMS = {"lender", "infra_fund"}
    EQUITY_FIRMS = {"vc", "pe_firm", "growth_equity", "family_office", "angel_network"}
    NOW = datetime.now(timezone.utc)

    candidates = []
    rejected = {"debt": 0, "equity": 0, "sector": 0, "geo": 0, "passed": 0}

    for r in rows:
        firm_type = r["firm_type"]
        ext_sectors = set(jsonl(r["extracted_sectors"]))
        ext_geos = set(jsonl(r["extracted_geographies"]))
        firm_sectors_text = (r["firm_sectors"] or "").lower()
        mandate_text = (r["mandate_descriptions"] or "").lower() + " " + (r["firm_strategy"] or "").lower()

        # ---- HARD FILTERS ----
        if args.debt:
            if not (r["accepts_debt"] == 1 or firm_type in DEBT_FIRMS):
                rejected["debt"] += 1; continue
        if args.equity:
            if not (r["accepts_equity"] == 1 or firm_type in EQUITY_FIRMS):
                rejected["equity"] += 1; continue
        if args.sector:
            sect = args.sector.lower()
            related = RELATED_SECTORS.get(sect, set())
            keywords = SECTOR_KEYWORDS.get(sect, [sect])
            has_match = (
                sect in ext_sectors
                or any(s in ext_sectors for s in related)
                or any(k in firm_sectors_text for k in keywords)
                or any(k in mandate_text for k in keywords)
            )
            if not has_match:
                rejected["sector"] += 1; continue
        if args.geo:
            geo = args.geo.lower()
            ok = (
                geo in ext_geos
                or "global" in ext_geos
                or (geo == "us" and (r["apollo_hq_country"] == "United States" or r["hq_country"] == "us"))
            )
            # If extracted_geographies is empty (unknown), don't reject — we'd be filtering on absence of data
            if not ok and ext_geos:
                rejected["geo"] += 1; continue
        if not args.include_passed and r["last_engagement_status"] == "passed":
            rejected["passed"] += 1; continue

        # ---- SOFT RANK ----
        score = 0.0
        why = []

        # Sector match strength
        if args.sector:
            sect = args.sector.lower()
            if sect in ext_sectors:
                score += 4; why.append(f"sector:{sect}=+4")
            else:
                rel_hits = ext_sectors & RELATED_SECTORS.get(sect, set())
                rel = min(2, len(rel_hits))
                if rel:
                    score += rel * 2; why.append(f"sector:related({','.join(sorted(rel_hits))})=+{rel*2}")
            if any(k in mandate_text for k in SECTOR_KEYWORDS.get(sect, [])):
                score += 2; why.append("sector:mandate-text=+2")

        # Debt quality
        if args.debt:
            if r["accepts_project_finance"]: score += 3; why.append("accepts_project_finance=+3")
            if r["accepts_credit"]:          score += 2; why.append("accepts_credit=+2")
            for k in ("senior debt", "project finance", "credit", "private credit"):
                if k in mandate_text:
                    score += 2; why.append(f"text:{k}=+2"); break

        # Geography
        if "us" in ext_geos: score += 3; why.append("geo:us=+3")
        if r["apollo_hq_country"] == "United States": score += 2; why.append("apollo_us=+2")

        # Relationship warmth (Attio)
        cs_score = r["attio_connection_strength_score"]
        if cs_score == 5:   score += 5; why.append("attio:VeryStrong=+5")
        elif cs_score == 4: score += 4; why.append("attio:Strong=+4")
        elif cs_score == 3: score += 3; why.append("attio:Good=+3")
        elif cs_score == 2: score += 1; why.append("attio:Weak=+1")

        # Recency
        last_int = parse_iso(r["attio_last_interaction"])
        if last_int:
            days = (NOW - last_int.replace(tzinfo=timezone.utc)).days
            if days <= 90:   score += 2; why.append(f"attio_recent_{days}d=+2")
            elif days <= 365: score += 1; why.append(f"attio_<1y=+1")

        # Prior SDP engagement state
        last_status = r["last_engagement_status"]
        if last_status in ("deck_sent", "meeting_booked", "second_meeting", "inquiry"):
            score += 3; why.append(f"engagement:{last_status}=+3")
        elif last_status == "followup":
            score += 2; why.append("engagement:followup=+2")
        elif last_status == "no_response":
            score -= 1; why.append("engagement:no_response=-1")

        # Contact present
        if r["contact_email"]:
            score += 1; why.append("has_email=+1")

        # Mandate signal density
        sig = r["mandate_signal_score"] or 0
        if sig > 0:
            bonus = round(sig * 2, 2)
            score += bonus; why.append(f"signal_score({sig})=+{bonus}")

        candidates.append({
            "score": round(score, 1),
            "firm": r["firm"],
            "firm_type": firm_type,
            "contact_name": r["contact_name"],
            "contact_email": r["contact_email"],
            "contact_title": r["contact_title"],
            "relationship_owner": r["relationship_owner"],
            "last_engagement_status": last_status,
            "last_sdp_client": r["last_sdp_client"],
            "last_feedback": r["last_feedback"],
            "attio_connection_strength": r["attio_connection_strength"],
            "attio_last_interaction": r["attio_last_interaction"],
            "mandate_descriptions": r["mandate_descriptions"],
            "firm_strategy": r["firm_strategy"],
            "extracted_sectors": jsonl(r["extracted_sectors"]),
            "extracted_geographies": jsonl(r["extracted_geographies"]),
            "why": why,
        })

    candidates.sort(key=lambda c: -c["score"])
    print(f"\nCriteria:  debt={args.debt}  equity={args.equity}  sector={args.sector}  geo={args.geo}")
    print(f"Total firm × contact rows scanned: {len(rows)}")
    print(f"Rejected: {rejected}")
    print(f"Surviving candidates: {len(candidates)}")
    print(f"\n=== TOP {args.top} ===\n")

    for i, c in enumerate(candidates[: args.top], 1):
        star = "★" if i <= args.top else " "
        print(f"{star} #{i}  score={c['score']:.1f}   {c['firm']}  ({c['firm_type'] or '-'})")
        if c["contact_name"]:
            owner = f"  owner={c['relationship_owner']}" if c["relationship_owner"] else ""
            print(f"     contact: {c['contact_name']} — {c['contact_title'] or 'no title'}{owner}")
            if c["contact_email"]:
                print(f"     email: {c['contact_email']}")
        else:
            print(f"     (no contact on file)")
        if c["attio_connection_strength"]:
            print(f"     attio: {c['attio_connection_strength']}, last={c['attio_last_interaction'] or '-'}")
        if c["last_engagement_status"]:
            extra = f" (for {c['last_sdp_client']})" if c["last_sdp_client"] else ""
            print(f"     engagement: {c['last_engagement_status']}{extra}")
            if c["last_feedback"]:
                fb = c["last_feedback"][:120].replace("\n", " ")
                print(f"     feedback: {fb}")
        if c["mandate_descriptions"]:
            m = c["mandate_descriptions"][:200].replace("\n", " ")
            print(f"     mandate: {m}")
        print(f"     sectors={c['extracted_sectors']}  geos={c['extracted_geographies']}")
        print(f"     score-breakdown: {' | '.join(c['why'])}")
        print()

    if len(candidates) > args.top:
        print(f"-- (showing {args.top} of {len(candidates)} qualifying candidates; pass --top N for more) --")
    conn.close()


if __name__ == "__main__":
    main()
