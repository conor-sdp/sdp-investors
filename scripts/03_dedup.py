"""
Phase 3: Collapse staging_* into canonical firms / contacts / mandates.

Merge keys:
  firms    : (name_normalized, domain) — domain is a tiebreaker, not a requirement
  contacts : email > linkedin > (firm_id + normalized full_name)
  mandates : not deduped (mandates accumulate; same firm can have multiple)

For each merge:
  - select 'best' field values (longest non-null, most-frequent slug)
  - union list-of-slug fields (sectors / stages)
  - append distinct name variants to firms.name_aliases
  - write a sources row per merged staging row
  - if a non-identity field disagrees across sources, push a dedup_conflict
    to review_queue with all candidate values (don't auto-resolve)

Idempotent: clears firms/contacts/mandates/sources before re-running.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent))
from _lib import connect, new_ulid, queue_review, write_source, check_size_bucket


def best_str(values: list[str | None]) -> str | None:
    """Pick the longest non-empty string. Used for free-text fields."""
    pool = [v for v in values if v]
    if not pool:
        return None
    return max(pool, key=len)


def majority_slug(values: list[str | None]) -> str | None:
    """Most frequent non-null slug; ties broken by first-occurrence."""
    pool = [v for v in values if v]
    if not pool:
        return None
    c = Counter(pool)
    return c.most_common(1)[0][0]


def union_jsonlist(values: list[str]) -> list[str]:
    """Union the elements across stringified JSON lists."""
    out = set()
    for v in values:
        if not v:
            continue
        try:
            lst = json.loads(v)
        except (ValueError, TypeError):
            continue
        for item in lst:
            if item:
                out.add(item)
    return sorted(out)


def best_numeric(values):
    pool = [v for v in values if v is not None]
    if not pool:
        return None
    return min(pool), max(pool)


# ---------------------------------------------------------------------
# Firm dedup
# ---------------------------------------------------------------------
def dedup_firms(conn) -> dict[str, str]:
    """Merge staging_firms -> firms. Returns {staging_id -> firm_id}."""
    rows = conn.execute("SELECT * FROM staging_firms").fetchall()
    # Group by (name_normalized) first; refine with domain conflict detection
    groups: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        key = r["name_normalized"] or r["name_raw"].lower()
        groups[key].append(r)

    # Within each name-group, split if domains conflict (only if domain present on 2+ rows AND differ)
    refined_groups: list[list[sqlite3.Row]] = []
    for key, members in groups.items():
        domains = {m["domain"] for m in members if m["domain"]}
        if len(domains) > 1:
            # Split into sub-groups by domain
            by_dom: dict[str, list[sqlite3.Row]] = defaultdict(list)
            no_dom: list[sqlite3.Row] = []
            for m in members:
                if m["domain"]:
                    by_dom[m["domain"]].append(m)
                else:
                    no_dom.append(m)
            for d, ms in by_dom.items():
                refined_groups.append(ms)
            # Rows with no domain stay attached to the largest domain group
            if no_dom:
                target = max(by_dom.values(), key=len) if by_dom else no_dom
                if target is no_dom:
                    refined_groups.append(no_dom)
                else:
                    target.extend(no_dom)
        else:
            refined_groups.append(members)

    staging_to_firm: dict[str, str] = {}
    for group in refined_groups:
        firm_id = new_ulid()
        names_raw = [m["name_raw"] for m in group]
        # Canonical name: longest, most-frequent
        name_counter = Counter(names_raw)
        # Pick most-frequent, ties broken by length
        name_canonical = max(name_counter.items(), key=lambda kv: (kv[1], len(kv[0])))[0]
        aliases = sorted({n for n in names_raw if n != name_canonical})

        domain = majority_slug([m["domain"] for m in group])
        url = best_str([m["url"] for m in group])
        type_slug = majority_slug([m["type"] for m in group])
        hq_city = best_str([m["hq_city"] for m in group])
        hq_country = majority_slug([m["hq_country"] for m in group])
        sectors = union_jsonlist([m["sectors"] for m in group])
        stages = union_jsonlist([m["stages"] for m in group])
        strategy = best_str([m["strategy"] for m in group])
        enrich = "complete" if any(m["enrichment_status"] == "complete" for m in group) else "pending"

        # Conflict detection on type slug
        type_set = {m["type"] for m in group if m["type"]}
        if len(type_set) > 1:
            queue_review(
                conn, "ambiguous_merge", name_canonical, "firm_type",
                type_slug, 0.0,
                {"firm_id": firm_id, "name_canonical": name_canonical,
                 "candidates": sorted(type_set),
                 "sources": [(m["source_file"], m["source_sheet"], m["source_row"]) for m in group]},
            )

        conn.execute(
            """INSERT INTO firms (firm_id, name_canonical, name_aliases, domain, url, type,
                  hq_city, hq_country, sectors, stages, strategy, enrichment_status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (firm_id, name_canonical, json.dumps(aliases), domain, url, type_slug,
             hq_city, hq_country, json.dumps(sectors), json.dumps(stages),
             strategy, enrich),
        )

        for m in group:
            staging_to_firm[m["staging_id"]] = firm_id
            write_source(conn, m["source_file"], m["source_sheet"], m["source_row"],
                         "firm", firm_id, json.loads(m["raw_payload"] or "{}"))

    return staging_to_firm


# ---------------------------------------------------------------------
# Contact dedup
# ---------------------------------------------------------------------
def dedup_contacts(conn, staging_to_firm: dict[str, str]) -> dict[str, str]:
    """Merge staging_contacts -> contacts. Returns {staging_id -> contact_id}."""
    rows = conn.execute("SELECT * FROM staging_contacts").fetchall()

    # Resolve each row to firm_id via the staging firm sharing source_file+source_row
    # (every contact was inserted alongside its firm at the same row)
    row_to_firm = {}
    sf_by_row = {
        (r["source_file"], r["source_sheet"], r["source_row"]): staging_to_firm.get(r["staging_id"])
        for r in conn.execute("SELECT staging_id, source_file, source_sheet, source_row FROM staging_firms").fetchall()
    }
    for r in rows:
        firm_id = sf_by_row.get((r["source_file"], r["source_sheet"], r["source_row"]))
        row_to_firm[r["staging_id"]] = firm_id

    # Group:
    #   1. by email if present
    #   2. by (firm_id + normalized full_name) for the rest
    groups: list[list[sqlite3.Row]] = []
    seen_ids = set()
    by_email: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        if r["email"]:
            by_email[r["email"]].append(r)
    for em, members in by_email.items():
        groups.append(members)
        for m in members:
            seen_ids.add(m["staging_id"])

    remaining = [r for r in rows if r["staging_id"] not in seen_ids]
    by_name_firm: dict[tuple, list[sqlite3.Row]] = defaultdict(list)
    for r in remaining:
        firm_id = row_to_firm.get(r["staging_id"])
        name_key = (r["full_name"] or "").lower().strip()
        if firm_id and name_key:
            by_name_firm[(firm_id, name_key)].append(r)
        else:
            # Solo: own group
            groups.append([r])
            seen_ids.add(r["staging_id"])
    for k, members in by_name_firm.items():
        groups.append(members)

    staging_to_contact: dict[str, str] = {}
    for group in groups:
        contact_id = new_ulid()

        # Pick firm_id (should be consistent; if not, flag)
        firm_ids = {row_to_firm.get(m["staging_id"]) for m in group}
        firm_ids.discard(None)
        if not firm_ids:
            # Orphan contact — skip; will be flagged in QA
            continue
        if len(firm_ids) > 1:
            queue_review(
                conn, "dedup_conflict",
                group[0]["email"] or group[0]["full_name"], "firm_id",
                None, 0.0,
                {"contact_id": contact_id, "competing_firm_ids": sorted(firm_ids),
                 "email": group[0]["email"], "full_name": group[0]["full_name"]},
            )
            firm_id = sorted(firm_ids)[0]  # deterministic
        else:
            firm_id = firm_ids.pop()

        first = best_str([m["first_name"] for m in group])
        last = best_str([m["last_name"] for m in group])
        full = best_str([m["full_name"] for m in group])
        title = best_str([m["title"] for m in group])
        email = next((m["email"] for m in group if m["email"]), None)
        linkedin = best_str([m["linkedin"] for m in group])
        seniority = majority_slug([m["seniority"] for m in group])
        role = majority_slug([m["relationship_owner"] for m in group])  # placeholder
        bio = best_str([m["bio"] for m in group])
        notes = best_str([m["notes"] for m in group])
        ro = majority_slug([m["relationship_owner"] for m in group])

        # Conflict: differing titles for same email
        title_set = {m["title"] for m in group if m["title"]}
        if len(title_set) > 1:
            queue_review(
                conn, "ambiguous_merge", email or full, "title",
                title, 0.0,
                {"contact_id": contact_id, "candidates": sorted(title_set),
                 "sources": [(m["source_file"], m["source_row"]) for m in group]},
            )

        conn.execute(
            """INSERT INTO contacts (contact_id, firm_id, first_name, last_name, full_name,
                  title, email, phone, linkedin, seniority, role,
                  last_contacted_at, relationship_owner, bio, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (contact_id, firm_id, first, last, full, title, email, None, linkedin,
             seniority, None, None, ro, bio, notes),
        )

        for m in group:
            staging_to_contact[m["staging_id"]] = contact_id
            write_source(conn, m["source_file"], m["source_sheet"], m["source_row"],
                         "contact", contact_id, json.loads(m["raw_payload"] or "{}"))

    return staging_to_contact


# ---------------------------------------------------------------------
# Mandates (no dedup — each row stands alone)
# ---------------------------------------------------------------------
def insert_mandates(conn, staging_to_firm: dict[str, str]) -> int:
    rows = conn.execute("SELECT * FROM staging_mandates").fetchall()
    # Resolve via firm_norm == staging_firms.name_normalized
    firm_by_norm = {
        r["name_normalized"]: staging_to_firm.get(r["staging_id"])
        for r in conn.execute("SELECT staging_id, name_normalized FROM staging_firms").fetchall()
    }
    inserted = 0
    for r in rows:
        firm_id = firm_by_norm.get(r["firm_name_raw"])
        if not firm_id:
            continue
        mandate_id = new_ulid()
        conn.execute(
            """INSERT INTO mandates (mandate_id, firm_id, description, sectors, stages,
                  check_size_min_usd_m, check_size_max_usd_m, geographies, active, source)
               VALUES (?,?,?,?,?,?,?,?,1,?)""",
            (mandate_id, firm_id, r["description"], r["sectors"], r["stages"],
             r["check_size_min_usd_m"], r["check_size_max_usd_m"], "[]", r["source"]),
        )
        write_source(conn, r["source_file"], r["source_sheet"], r["source_row"],
                     "mandate", mandate_id, json.loads(r["raw_payload"] or "{}"))
        inserted += 1
    return inserted


# ---------------------------------------------------------------------
# Backfill check_size_bucket on firms
# ---------------------------------------------------------------------
def backfill_check_size_buckets(conn):
    rows = conn.execute("SELECT firm_id, firm_id FROM firms").fetchall()
    # Derive firm-level check_size from mandates
    for fid in [r[0] for r in rows]:
        agg = conn.execute(
            "SELECT MIN(check_size_min_usd_m), MAX(check_size_max_usd_m) FROM mandates WHERE firm_id=?",
            (fid,),
        ).fetchone()
        lo, hi = agg[0], agg[1]
        bucket = check_size_bucket(lo, hi)
        conn.execute(
            "UPDATE firms SET check_size_min_usd_m=?, check_size_max_usd_m=?, check_size_bucket=? WHERE firm_id=?",
            (lo, hi, bucket, fid),
        )


def main():
    conn = connect()
    # Idempotency: clear canonical tables (sources too) — staging is the source of truth
    print("Clearing canonical tables ...")
    for tbl in ("co_investments", "engagements", "portfolio_companies", "mandates",
                "contacts", "firms", "sources"):
        conn.execute(f"DELETE FROM {tbl}")
    # Keep review_queue rows from Phase 2 (still relevant); we'll add to it here.
    conn.commit()

    print("\nDeduping firms ...")
    s2f = dedup_firms(conn)
    n_firms = conn.execute("SELECT COUNT(*) FROM firms").fetchone()[0]
    print(f"  staging_firms: {len(s2f)} rows -> firms: {n_firms}")

    print("\nDeduping contacts ...")
    s2c = dedup_contacts(conn, s2f)
    n_contacts = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
    print(f"  staging_contacts: {len(s2c)} mapped rows -> contacts: {n_contacts}")

    print("\nInserting mandates ...")
    n_mandates = insert_mandates(conn, s2f)
    print(f"  mandates inserted: {n_mandates}")

    print("\nBackfilling check_size buckets on firms ...")
    backfill_check_size_buckets(conn)
    bucket_counts = conn.execute(
        "SELECT check_size_bucket, COUNT(*) FROM firms GROUP BY check_size_bucket"
    ).fetchall()
    for b, c in bucket_counts:
        print(f"  {b or 'unknown':<10} {c}")

    conn.commit()

    # Persist staging -> canonical id map for downstream phases
    sf_map_path = Path(__file__).resolve().parent.parent / "db" / "_staging_to_canonical.json"
    sf_map_path.write_text(json.dumps({"firms": s2f, "contacts": s2c}))
    print(f"\nWrote {sf_map_path.relative_to(Path(__file__).resolve().parent.parent.parent)}")

    # Summary
    print("\n" + "=" * 78)
    print("PHASE 3 DEDUP SUMMARY")
    print("=" * 78)
    print(f"  firms (canonical):   {n_firms} (from {len(s2f)} staging rows; collapsed {len(s2f) - n_firms})")
    print(f"  contacts:            {n_contacts} (from {len(s2c)} mapped)")
    print(f"  mandates:            {n_mandates}")
    rq = conn.execute("SELECT category, COUNT(*) FROM review_queue WHERE status='open' GROUP BY 1").fetchall()
    print(f"  review_queue open:")
    for cat, n in rq:
        print(f"    {cat:<20} {n}")

    conn.close()


if __name__ == "__main__":
    main()
