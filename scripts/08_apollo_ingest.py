"""
Ingest Apollo bulk_enrich responses into firms.apollo_*.

Reads every file in enrichment_cache/apollo/batch_*.json. Each file is
the raw JSON response from a single mcp__apollo_organizations_bulk_enrich
call (one or both keys: 'organizations', and possibly 'matched'/'unmatched').

For each org in the response:
  - look up firm_id via domain (primary_domain, website_url, or sanitized name)
  - update firms.apollo_* fields
  - persist the raw payload to enrichment_cache/apollo/{firm_id}.json
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "db" / "investors.db"
CACHE = ROOT / "enrichment_cache" / "apollo"


def clean_domain(d):
    if not d:
        return None
    s = str(d).strip().lower()
    s = re.sub(r"^https?://", "", s)
    s = s.split("/", 1)[0]
    s = re.sub(r"^www\.", "", s)
    return s or None


def org_to_row(org: dict) -> dict:
    """Extract the columns we care about from one organization payload."""
    addr = org.get("primary_phone") or {}
    org_industries = org.get("industries") or []
    if isinstance(org_industries, str):
        org_industries = [org_industries]
    keywords = org.get("keywords") or []
    if isinstance(keywords, str):
        keywords = [keywords]
    funding_events = org.get("funding_events") or []
    latest_stage = None
    if funding_events:
        # newest first if sorted; otherwise just take max by date
        try:
            latest_stage = max(funding_events, key=lambda e: e.get("date", ""))["type"]
        except Exception:
            latest_stage = funding_events[0].get("type")
    return {
        "apollo_id":               org.get("id"),
        "apollo_name":             org.get("name"),
        "apollo_website":          org.get("website_url") or org.get("primary_domain"),
        "apollo_description":      org.get("short_description") or org.get("description"),
        "apollo_short_description": org.get("short_description"),
        "apollo_industry":         org.get("industry"),
        "apollo_industries":       json.dumps(org_industries) if org_industries else None,
        "apollo_keywords":         json.dumps(keywords) if keywords else None,
        "apollo_employee_count":   org.get("estimated_num_employees"),
        "apollo_employee_range":   org.get("organization_headcount"),
        "apollo_annual_revenue":   org.get("annual_revenue"),
        "apollo_founded_year":     org.get("founded_year"),
        "apollo_linkedin_url":     org.get("linkedin_url"),
        "apollo_twitter_url":      org.get("twitter_url"),
        "apollo_facebook_url":     org.get("facebook_url"),
        "apollo_phone":            (org.get("primary_phone") or {}).get("number") if isinstance(org.get("primary_phone"), dict) else org.get("phone"),
        "apollo_hq_street":        org.get("street_address"),
        "apollo_hq_city":          org.get("city"),
        "apollo_hq_state":         org.get("state"),
        "apollo_hq_country":       org.get("country"),
        "apollo_total_funding":    org.get("total_funding"),
        "apollo_latest_funding_stage": latest_stage,
        "apollo_status":           "matched",
        "apollo_enriched_at":      datetime.now(timezone.utc).isoformat(),
    }


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    # Domain -> [firm_id, ...] from the prep step
    dmap = json.loads((CACHE / "_domain_to_firm.json").read_text())
    batches = json.loads((CACHE / "_batches.json").read_text())

    # Track only domains that were actually attempted (= in a batch_*.json on disk)
    attempted_domains = set()
    for path in sorted(CACHE.glob("batch_*.json")):
        try:
            idx = int(path.stem.replace("batch_", ""))
            for d in batches[idx]:
                attempted_domains.add(d.lower())
        except (ValueError, IndexError):
            pass

    matched, unmatched, files = 0, 0, 0
    matched_domains = set()
    for path in sorted(CACHE.glob("batch_*.json")):
        files += 1
        try:
            data = json.loads(path.read_text())
        except Exception as e:
            print(f"  !! could not parse {path.name}: {e}")
            continue
        # Apollo bulk_enrich responses contain key 'organizations' (list).
        orgs = data.get("organizations") or data.get("matches") or []
        for org in orgs:
            if not org:    # null entries = Apollo couldn't match the domain
                continue
            cand_doms = []
            for k in ("primary_domain", "website_url", "domain"):
                v = clean_domain(org.get(k))
                if v:
                    cand_doms.append(v)
            firm_ids = []
            for d in cand_doms:
                if d in dmap:
                    firm_ids.extend(fm["firm_id"] for fm in dmap[d])
                    matched_domains.add(d)
                    break
            if not firm_ids:
                unmatched += 1
                continue
            row = org_to_row(org)
            cols = ", ".join(f"{k}=?" for k in row.keys())
            for fid in firm_ids:
                conn.execute(f"UPDATE firms SET {cols} WHERE firm_id=?",
                             list(row.values()) + [fid])
                matched += 1
                # Per-firm cache
                (CACHE / f"{fid}.json").write_text(json.dumps(org, default=str))

    # Mark attempted-but-unmatched domains as not_found
    # (NOT all of dmap — only domains in batches we actually processed)
    not_found = 0
    for d in attempted_domains:
        if d in matched_domains:
            continue
        for fm in dmap.get(d, []):
            n = conn.execute(
                "UPDATE firms SET apollo_status='not_found', apollo_enriched_at=? "
                "WHERE firm_id=? AND apollo_status IN ('matched','not_found') = 0",
                (datetime.now(timezone.utc).isoformat(), fm["firm_id"]),
            ).rowcount
            not_found += n

    conn.commit()

    n_matched = conn.execute("SELECT COUNT(*) FROM firms WHERE apollo_status='matched'").fetchone()[0]
    n_not_found = conn.execute("SELECT COUNT(*) FROM firms WHERE apollo_status='not_found'").fetchone()[0]
    print(f"batches read:                     {files}")
    print(f"firm rows updated (matched):      {matched}")
    print(f"firm rows updated (not_found):    {not_found}")
    print(f"unmatched orgs in responses:      {unmatched}")
    print(f"---")
    print(f"firms.apollo_status='matched':    {n_matched}")
    print(f"firms.apollo_status='not_found':  {n_not_found}")
    conn.close()


if __name__ == "__main__":
    main()
