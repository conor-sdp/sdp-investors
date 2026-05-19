"""
Phase 5: Build network edges.

  engagements      : merge staging_engagements into canonical engagements
                     (linked to canonical firm + contact via the staging->canonical map).
  co_investments   : derive from portfolio_companies where 2+ firms share a normalized
                     company name. Stored once per unordered pair with a_firm_id<b_firm_id.

If portfolio_companies is empty (no LLM-based enrichment yet), co_investments
will be empty too — that is expected without ANTHROPIC_API_KEY.
"""
from __future__ import annotations

import json
from collections import defaultdict
from itertools import combinations
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent))
from _lib import connect, new_ulid, write_source


def main():
    conn = connect()

    # ---- Engagements ----
    # We need staging -> canonical firm_id + contact_id maps.
    # firms map by source_file+source_sheet+source_row (every staging firm row was 1:1 with a row).
    # contacts map similarly.
    print("Linking engagements to canonical firms + contacts ...")
    # Source-row maps
    firm_by_row: dict[tuple, str] = {}
    for r in conn.execute(
        """SELECT sf.source_file, sf.source_sheet, sf.source_row, s.entity_id
           FROM staging_firms sf
           JOIN sources s ON s.source_file = sf.source_file
                          AND ((s.source_sheet IS NULL AND sf.source_sheet IS NULL)
                               OR s.source_sheet = sf.source_sheet)
                          AND s.source_row = sf.source_row
                          AND s.entity_type = 'firm'"""
    ).fetchall():
        firm_by_row[(r["source_file"], r["source_sheet"], r["source_row"])] = r["entity_id"]

    contact_by_row: dict[tuple, str] = {}
    for r in conn.execute(
        """SELECT sc.source_file, sc.source_sheet, sc.source_row, s.entity_id
           FROM staging_contacts sc
           JOIN sources s ON s.source_file = sc.source_file
                          AND ((s.source_sheet IS NULL AND sc.source_sheet IS NULL)
                               OR s.source_sheet = sc.source_sheet)
                          AND s.source_row = sc.source_row
                          AND s.entity_type = 'contact'"""
    ).fetchall():
        contact_by_row[(r["source_file"], r["source_sheet"], r["source_row"])] = r["entity_id"]

    inserted = 0
    skipped = 0
    for r in conn.execute("SELECT * FROM staging_engagements").fetchall():
        key = (r["source_file"], r["source_sheet"], r["source_row"])
        firm_id = firm_by_row.get(key)
        contact_id = contact_by_row.get(key)
        if not firm_id:
            skipped += 1
            continue
        engagement_id = new_ulid()
        conn.execute(
            """INSERT INTO engagements (engagement_id, contact_id, firm_id, sdp_client,
                  mandate_pitched, date, channel, status, feedback, feedback_secondary,
                  notes, followup, meeting_held, smartlead_link, responded_by)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (engagement_id, contact_id, firm_id, r["sdp_client"], None,
             r["date"], r["channel"], r["status"] or "no_response",
             r["feedback"], r["feedback_secondary"], r["notes"],
             r["followup"], r["meeting_held"], r["smartlead_link"], r["responded_by"]),
        )
        write_source(conn, r["source_file"], r["source_sheet"], r["source_row"],
                     "engagement", engagement_id,
                     json.loads(r["raw_payload"] or "{}"))
        inserted += 1
    print(f"  engagements inserted: {inserted}")
    if skipped:
        print(f"  skipped (orphan firm): {skipped}")

    # ---- Co-investments ----
    print("\nDeriving co-investment edges from portfolio_companies ...")
    rows = conn.execute(
        "SELECT firm_id, company_name_normalized FROM portfolio_companies"
    ).fetchall()
    by_company: dict[str, set[str]] = defaultdict(set)
    by_company_names: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        by_company[r["company_name_normalized"]].add(r["firm_id"])
    # collect display names too
    for r in conn.execute("SELECT firm_id, company_name, company_name_normalized FROM portfolio_companies").fetchall():
        by_company_names[r["company_name_normalized"]].add(r["company_name"])

    pair_shared: dict[tuple[str, str], list[str]] = defaultdict(list)
    for company_norm, firm_set in by_company.items():
        if len(firm_set) < 2:
            continue
        for a, b in combinations(sorted(firm_set), 2):
            pair_shared[(a, b)].append(company_norm)

    edges = 0
    for (a, b), companies in pair_shared.items():
        if len(companies) < 2:
            continue
        # store display names (best one per normalized name)
        display = []
        for cn in companies:
            names = by_company_names.get(cn, set())
            display.append(max(names, key=len) if names else cn)
        conn.execute(
            """INSERT INTO co_investments (a_firm_id, b_firm_id, shared_company_count, shared_companies)
               VALUES (?,?,?,?)""",
            (a, b, len(companies), json.dumps(sorted(display))),
        )
        edges += 1
    print(f"  co-investment edges: {edges}")
    if not rows:
        print("  (no portfolio_companies — LLM extraction not yet run)")

    conn.commit()

    # Summary
    print("\n" + "=" * 78)
    print("PHASE 5 NETWORK SUMMARY")
    print("=" * 78)
    n_eng = conn.execute("SELECT COUNT(*) FROM engagements").fetchone()[0]
    n_pc = conn.execute("SELECT COUNT(*) FROM portfolio_companies").fetchone()[0]
    n_ci = conn.execute("SELECT COUNT(*) FROM co_investments").fetchone()[0]
    print(f"  engagements:         {n_eng}")
    print(f"  portfolio_companies: {n_pc}")
    print(f"  co_investments:      {n_ci}")
    print()
    print("  Top engagement statuses:")
    for row in conn.execute(
        "SELECT status, COUNT(*) c FROM engagements GROUP BY 1 ORDER BY 2 DESC LIMIT 8"
    ).fetchall():
        print(f"    {row['status']:<20} {row['c']:>4}")
    print()
    print("  Engagements by SDP client:")
    for row in conn.execute(
        "SELECT COALESCE(sdp_client,'(none)') c, COUNT(*) n FROM engagements GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall():
        print(f"    {row['c']:<25} {row['n']:>4}")

    conn.close()


if __name__ == "__main__":
    main()
