"""
Pure scoring function. Imported by both best_contacts.py (CLI) and app.py (Streamlit).

Given a criteria dict, returns a list of scored candidates sorted desc by score.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

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

DEBT_FIRMS = {"lender", "infra_fund"}
EQUITY_FIRMS = {"vc", "pe_firm", "growth_equity", "family_office", "angel_network"}


@dataclass
class ScoringCriteria:
    require_debt: bool = False
    require_equity: bool = False
    primary_sector: Optional[str] = None
    secondary_sectors: list[str] = field(default_factory=list)
    geographies: list[str] = field(default_factory=list)
    include_passed: bool = False
    extra_keywords: list[str] = field(default_factory=list)
    dedupe_by_firm: bool = True   # one row per firm; best contact surfaced, rest as alternates


def _jsonl(s):
    if not s:
        return []
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return []


def _parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def score_candidates(conn: sqlite3.Connection, criteria: ScoringCriteria) -> tuple[list[dict], dict]:
    """Returns (candidates_sorted_desc, rejection_counts)."""
    rows = conn.execute("SELECT * FROM v_relationships").fetchall()
    rejected = {"debt": 0, "equity": 0, "sector": 0, "geo": 0, "passed": 0}
    candidates: list[dict] = []
    now = datetime.now(timezone.utc)

    sector_set = {s for s in [criteria.primary_sector, *criteria.secondary_sectors] if s}
    geo_set = {g for g in criteria.geographies if g}

    for r in rows:
        firm_type = r["firm_type"]
        ext_sectors = set(_jsonl(r["extracted_sectors"]))
        ext_geos = set(_jsonl(r["extracted_geographies"]))
        firm_sectors_text = (r["firm_sectors"] or "").lower()
        mandate_text = (r["mandate_descriptions"] or "").lower() + " " + (r["firm_strategy"] or "").lower()
        for kw in criteria.extra_keywords:
            mandate_text += " " + kw.lower()

        # ---- HARD FILTERS ----
        if criteria.require_debt and not (r["accepts_debt"] == 1 or firm_type in DEBT_FIRMS):
            rejected["debt"] += 1; continue
        if criteria.require_equity and not (r["accepts_equity"] == 1 or firm_type in EQUITY_FIRMS):
            rejected["equity"] += 1; continue
        if sector_set:
            has_match = False
            for sect in sector_set:
                related = RELATED_SECTORS.get(sect, set())
                keywords = SECTOR_KEYWORDS.get(sect, [sect])
                if (sect in ext_sectors
                        or any(s in ext_sectors for s in related)
                        or any(k in firm_sectors_text for k in keywords)
                        or any(k in mandate_text for k in keywords)):
                    has_match = True; break
            if not has_match:
                rejected["sector"] += 1; continue
        if geo_set:
            ok = False
            for geo in geo_set:
                if (geo in ext_geos
                        or "global" in ext_geos
                        or (geo == "us" and (r["apollo_hq_country"] == "United States" or r["hq_country"] == "us"))):
                    ok = True; break
            # If we have NO geo data at all, don't reject — absence isn't evidence
            if not ok and ext_geos:
                rejected["geo"] += 1; continue
        if not criteria.include_passed and r["last_engagement_status"] == "passed":
            rejected["passed"] += 1; continue

        # ---- SOFT RANK ----
        score = 0.0
        why = []

        # Sector match strength (use primary as anchor)
        if criteria.primary_sector:
            sect = criteria.primary_sector
            if sect in ext_sectors:
                score += 4; why.append(f"sector:{sect}=+4")
            else:
                rel_hits = ext_sectors & RELATED_SECTORS.get(sect, set())
                if rel_hits:
                    rel = min(2, len(rel_hits))
                    score += rel * 2; why.append(f"sector:related({','.join(sorted(rel_hits))})=+{rel*2}")
            if any(k in mandate_text for k in SECTOR_KEYWORDS.get(sect, [])):
                score += 2; why.append("sector:mandate-text=+2")

        # Debt-quality
        if criteria.require_debt:
            if r["accepts_project_finance"]: score += 3; why.append("accepts_project_finance=+3")
            if r["accepts_credit"]:          score += 2; why.append("accepts_credit=+2")
            for k in ("senior debt", "project finance", "credit", "private credit"):
                if k in mandate_text:
                    score += 2; why.append(f"text:{k}=+2"); break

        # Geography
        if "us" in ext_geos:
            score += 3; why.append("geo:us=+3")
        if r["apollo_hq_country"] == "United States":
            score += 2; why.append("apollo_us=+2")

        # Relationship warmth (Attio)
        cs = r["attio_connection_strength_score"]
        if cs == 5:   score += 5; why.append("attio:VeryStrong=+5")
        elif cs == 4: score += 4; why.append("attio:Strong=+4")
        elif cs == 3: score += 3; why.append("attio:Good=+3")
        elif cs == 2: score += 1; why.append("attio:Weak=+1")

        # Recency
        last_int = _parse_iso(r["attio_last_interaction"])
        if last_int:
            days = (now - last_int.replace(tzinfo=timezone.utc)).days
            if days <= 90:    score += 2; why.append(f"attio_recent_{days}d=+2")
            elif days <= 365: score += 1; why.append("attio_<1y=+1")

        # Prior SDP engagement
        last_status = r["last_engagement_status"]
        if last_status in ("deck_sent", "meeting_booked", "second_meeting", "inquiry"):
            score += 3; why.append(f"engagement:{last_status}=+3")
        elif last_status == "followup":
            score += 2; why.append("engagement:followup=+2")
        elif last_status == "no_response":
            score -= 1; why.append("engagement:no_response=-1")

        if r["contact_email"]:
            score += 1; why.append("has_email=+1")

        sig = r["mandate_signal_score"] or 0
        if sig > 0:
            bonus = round(sig * 2, 2)
            score += bonus; why.append(f"signal_score({sig})=+{bonus}")

        candidates.append({
            "score": round(score, 1),
            "firm": r["firm"],
            "firm_id": r["firm_id"],
            "firm_type": firm_type,
            "contact_id": r["contact_id"],
            "contact_name": r["contact_name"],
            "contact_email": r["contact_email"],
            "contact_title": r["contact_title"],
            "contact_seniority": r["contact_seniority"],
            "relationship_owner": r["relationship_owner"],
            "last_engagement_status": last_status,
            "last_sdp_client": r["last_sdp_client"],
            "last_feedback": r["last_feedback"],
            "last_engagement_date": r["last_engagement_date"],
            "attio_connection_strength": r["attio_connection_strength"],
            "attio_last_interaction": r["attio_last_interaction"],
            "mandate_descriptions": r["mandate_descriptions"],
            "firm_strategy": r["firm_strategy"],
            "extracted_sectors": _jsonl(r["extracted_sectors"]),
            "extracted_geographies": _jsonl(r["extracted_geographies"]),
            "apollo_employee_count": r["apollo_employee_count"],
            "apollo_industry": r["apollo_industry"],
            "firm_linkedin": r["firm_linkedin"],
            "url": r["url"],
            "domain": r["domain"],
            "why": why,
        })

    candidates.sort(key=lambda c: -c["score"])

    if criteria.dedupe_by_firm:
        candidates = _collapse_to_firms(candidates)

    return candidates, rejected


# Contact-quality ranking — used to pick the best contact at a firm when
# multiple contacts share the same firm-level score. Higher is better.
_SENIORITY_RANK = {
    "founder": 100, "ceo": 95, "managing_partner": 90, "managing_director": 85,
    "partner": 80, "principal": 70, "cio": 75, "cfo": 65,
    "director": 60, "vp": 50, "associate": 35, "analyst": 25,
    "representative": 15, "other": 10,
}


def _contact_quality(c: dict) -> tuple:
    """Lex tuple, higher tuple = better contact. Used as tiebreaker
    within a firm when picking the representative contact."""
    return (
        1 if c.get("last_engagement_status") in (
            "deck_sent", "meeting_booked", "second_meeting", "inquiry",
            "followup", "pitched") else 0,
        1 if c.get("relationship_owner") else 0,
        1 if c.get("contact_email") else 0,
        _SENIORITY_RANK.get(c.get("contact_seniority") or "other", 0),
        # break ties by name alphabetically (stable, deterministic)
        -ord((c.get("contact_name") or "z")[:1].lower()),
    )


def _collapse_to_firms(candidates: list[dict]) -> list[dict]:
    """Group by firm_id. Keep highest-scoring row; attach other contacts
    at the same firm as `other_contacts`. Within a firm, the representative
    contact is the one with the best (engagement, ownership, email,
    seniority) tuple — not just the first scored."""
    by_firm: dict[str, list[dict]] = {}
    order: list[str] = []
    for c in candidates:
        fid = c["firm_id"]
        if fid not in by_firm:
            by_firm[fid] = []
            order.append(fid)
        by_firm[fid].append(c)

    out: list[dict] = []
    for fid in order:
        rows = by_firm[fid]
        # All firm rows share firm-level signals; tiebreak on contact quality
        rows.sort(key=lambda r: (-r["score"], -1 * sum(_contact_quality(r))))
        # Pick the row with the best contact (not just highest score —
        # they're all tied within a firm on firm-level signals)
        best = max(rows, key=lambda r: (r["score"], _contact_quality(r)))
        others = [r for r in rows if r is not best and r.get("contact_id")]
        best["other_contacts"] = [
            {
                "contact_id": o["contact_id"],
                "contact_name": o["contact_name"],
                "contact_email": o["contact_email"],
                "contact_title": o["contact_title"],
                "contact_seniority": o["contact_seniority"],
                "relationship_owner": o["relationship_owner"],
                "last_engagement_status": o["last_engagement_status"],
            }
            for o in others
            if o.get("contact_name") or o.get("contact_email")
        ]
        best["contact_count_at_firm"] = (
            (1 if best.get("contact_id") else 0) + len(best["other_contacts"])
        )
        out.append(best)
    out.sort(key=lambda c: -c["score"])
    return out
