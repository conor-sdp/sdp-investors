"""
Hardened "best firms for a deal" scorer (CLI).

One row per firm (target = firm). Best contact is surfaced; alternates
roll up as `other_contacts`. To get the old per-contact view, pass
`--no-dedupe`.

Usage:
    .venv/bin/python scripts/best_contacts.py --debt --sector solar --geo us --top 5
    .venv/bin/python scripts/best_contacts.py --equity --sector data_centers --top 10
    .venv/bin/python scripts/best_contacts.py --debt --sector solar --no-dedupe

Hard filters:
  --debt    : firm must accept debt (accepts_debt=1 OR firm_type IN lender,infra_fund)
  --equity  : firm must accept equity (accepts_equity=1 OR firm_type IN vc, pe_firm, …)
  --sector  : firm must list this slug (or related/keyword match)
  --geo us  : firm must list 'us' OR global, OR have no contrary geo signal

Soft rank weights live in scripts/_score.py.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import connect  # noqa: E402
from _score import ScoringCriteria, score_candidates  # noqa: E402


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--debt", action="store_true", help="Require firm accepts debt")
    p.add_argument("--equity", action="store_true", help="Require firm accepts equity")
    p.add_argument("--sector", type=str, default=None,
                   help="Required sector slug (e.g. solar, data_centers, infrastructure)")
    p.add_argument("--geo", type=str, default=None,
                   help="Required geography slug (e.g. us, canada, global)")
    p.add_argument("--top", type=int, default=5)
    p.add_argument("--include-passed", action="store_true",
                   help="Include firms whose latest engagement was 'passed'")
    p.add_argument("--no-dedupe", action="store_true",
                   help="Show one row per contact instead of one row per firm")
    return p.parse_args()


def main():
    args = parse_args()
    conn = connect()

    criteria = ScoringCriteria(
        require_debt=args.debt,
        require_equity=args.equity,
        primary_sector=args.sector,
        geographies=[args.geo] if args.geo else [],
        include_passed=args.include_passed,
        dedupe_by_firm=not args.no_dedupe,
    )
    cands, rejected = score_candidates(conn, criteria)
    conn.close()

    print(f"\nCriteria:  debt={args.debt}  equity={args.equity}  sector={args.sector}  geo={args.geo}")
    print(f"Rejected:  {dict(rejected)}")
    grain = "firm" if criteria.dedupe_by_firm else "firm × contact"
    print(f"Surviving {grain} candidates: {len(cands)}")
    print(f"\n=== TOP {args.top} ===\n")

    for i, c in enumerate(cands[: args.top], 1):
        print(f"#{i}  score={c['score']:.1f}   {c['firm']}  ({c['firm_type'] or '-'})")
        if c.get("contact_name"):
            owner = f"  owner={c['relationship_owner']}" if c.get("relationship_owner") else ""
            print(f"     primary contact: {c['contact_name']} — "
                  f"{c.get('contact_title') or 'no title'}{owner}")
            if c.get("contact_email"):
                print(f"     email: {c['contact_email']}")
        else:
            print(f"     (no contact on file)")
        if c.get("attio_connection_strength"):
            print(f"     attio: {c['attio_connection_strength']}, "
                  f"last={c.get('attio_last_interaction') or '-'}")
        if c.get("last_engagement_status"):
            extra = f" (for {c['last_sdp_client']})" if c.get("last_sdp_client") else ""
            print(f"     engagement: {c['last_engagement_status']}{extra}")
            if c.get("last_feedback"):
                fb = c["last_feedback"][:120].replace("\n", " ")
                print(f"     feedback: {fb}")
        if c.get("mandate_descriptions"):
            m = c["mandate_descriptions"][:200].replace("\n", " ")
            print(f"     mandate: {m}")
        # Roll-up of other contacts at this firm
        others = c.get("other_contacts") or []
        if others:
            print(f"     other contacts at firm ({len(others)}):")
            for o in others[:5]:
                owner = f" · owner={o['relationship_owner']}" if o.get("relationship_owner") else ""
                eng = f" · {o['last_engagement_status']}" if o.get("last_engagement_status") else ""
                email = f"  <{o['contact_email']}>" if o.get("contact_email") else ""
                print(f"       - {o['contact_name']} ({o.get('contact_title') or '-'}){owner}{eng}{email}")
            if len(others) > 5:
                print(f"       … and {len(others) - 5} more")
        print(f"     sectors={c.get('extracted_sectors')}  "
              f"geos={c.get('extracted_geographies')}")
        print(f"     score-breakdown: {' | '.join(c['why'])}")
        print()

    if len(cands) > args.top:
        print(f"-- showing {args.top} of {len(cands)} surviving {grain} candidates "
              f"(pass --top N for more)")


if __name__ == "__main__":
    main()
