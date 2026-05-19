"""
Phase 7: QA sanity checks. Writes findings to ingest_log.md and prints summary.

Checks:
  - PK uniqueness on firms.firm_id, contacts.contact_id (constraint-level, plus assert)
  - Every contact has a firm_id (FK integrity)
  - Email regex validates on all non-null contacts.email
  - Source row count reconciles with inventory.json
  - Count of firms by enrichment_status
  - Count of contacts merged vs total raw rows
  - Top-degree firms in co-investment graph (will be empty without portfolio data)
  - Three example queries to demonstrate the DB
"""
from __future__ import annotations

import json
import re
import socket
from datetime import datetime, timezone
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent))
from _lib import connect, ROOT


LOG = ROOT / "ingest_log.md"
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def header(s: str) -> str:
    return f"\n## {s}\n"


def main():
    conn = connect()
    findings = []
    findings.append(f"\n# QA report — {datetime.now(timezone.utc).isoformat()}\n")

    # --- Counts ---
    findings.append(header("Counts"))
    counts = {}
    for tbl in ("firms", "contacts", "mandates", "engagements", "portfolio_companies",
                "co_investments", "sources", "review_queue"):
        counts[tbl] = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        findings.append(f"- **{tbl}**: {counts[tbl]:,}")
    print("Counts:")
    for k, v in counts.items():
        print(f"  {k:<22} {v:>6,}")

    # --- FK / uniqueness ---
    findings.append(header("Integrity"))
    failures = []
    # PK uniqueness (sqlite enforces this; just count for completeness)
    n_firms = conn.execute("SELECT COUNT(DISTINCT firm_id) FROM firms").fetchone()[0]
    if n_firms != counts["firms"]:
        failures.append(f"firms.firm_id duplicates: {counts['firms'] - n_firms}")
    n_c = conn.execute("SELECT COUNT(DISTINCT contact_id) FROM contacts").fetchone()[0]
    if n_c != counts["contacts"]:
        failures.append(f"contacts.contact_id duplicates: {counts['contacts'] - n_c}")
    orphan_contacts = conn.execute(
        "SELECT COUNT(*) FROM contacts c WHERE NOT EXISTS (SELECT 1 FROM firms f WHERE f.firm_id=c.firm_id)"
    ).fetchone()[0]
    if orphan_contacts:
        failures.append(f"orphan contacts (no firm): {orphan_contacts}")
    # Emails
    bad_emails = conn.execute(
        "SELECT email FROM contacts WHERE email IS NOT NULL"
    ).fetchall()
    n_bad = sum(1 for r in bad_emails if not EMAIL_RE.match(r["email"] or ""))
    if n_bad:
        failures.append(f"contacts with invalid email regex: {n_bad}")
    findings.append(f"- PK / FK / email integrity: {'**all checks passed**' if not failures else 'failures:'}")
    for f in failures:
        findings.append(f"  - {f}")
    print(f"\nIntegrity checks: {'PASS' if not failures else 'FAIL'}")
    for f in failures:
        print(f"  - {f}")

    # --- Source reconciliation ---
    # `sources` is 1:N per ingested row (firm + contact + engagement per row).
    # Compare DISTINCT (source_file, source_sheet, source_row) tuples to raw XLSX
    # non-empty row counts.
    findings.append(header("Source row reconciliation"))
    inventory = json.loads((ROOT / "inventory.json").read_text())
    inv_files = {f["file"]: sum(s["row_count"] for s in f["sheets"]) for f in inventory["files"]}
    src_distinct = dict(conn.execute(
        """SELECT source_file, COUNT(DISTINCT source_file || '/' || COALESCE(source_sheet,'') || '/' || source_row)
           FROM sources GROUP BY 1"""
    ).fetchall())
    src_total = dict(conn.execute(
        "SELECT source_file, COUNT(*) FROM sources GROUP BY 1"
    ).fetchall())
    print("\nSource reconciliation:")
    print(f"  {'FILE':<55} {'XLSX_ROWS':>10} {'DISTINCT_SRC_ROWS':>18} {'TOTAL_SRC_RECS':>15}")
    for f, raw in sorted(inv_files.items()):
        d = src_distinct.get(f, 0)
        t = src_total.get(f, 0)
        line = f"  {f[:55]:<55} {raw:>10} {d:>18} {t:>15}"
        print(line)
        findings.append(f"- `{f}`: {raw} raw / {d} distinct ingested rows / {t} source records")

    # --- Enrichment status ---
    findings.append(header("Enrichment status"))
    es = conn.execute(
        "SELECT enrichment_status, COUNT(*) FROM firms GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall()
    print("\nEnrichment status:")
    for r in es:
        print(f"  {r[0]:<14} {r[1]:>4}")
        findings.append(f"- `{r[0]}`: {r[1]}")

    # --- DNS check on a sample of firm domains ---
    findings.append(header("DNS sanity on firm domains (sample of 25)"))
    sample = conn.execute(
        "SELECT firm_id, domain FROM firms WHERE domain IS NOT NULL ORDER BY RANDOM() LIMIT 25"
    ).fetchall()
    dns_ok = dns_fail = 0
    fails = []
    for r in sample:
        try:
            socket.gethostbyname(r["domain"])
            dns_ok += 1
        except OSError:
            dns_fail += 1
            fails.append(r["domain"])
    print(f"\nDNS resolution on 25-firm sample: {dns_ok} ok, {dns_fail} fail")
    findings.append(f"- DNS sample of 25: {dns_ok} ok / {dns_fail} fail")
    for f in fails[:5]:
        findings.append(f"  - failed: `{f}`")

    # --- Top firms by contact count ---
    findings.append(header("Top 15 firms by contact count"))
    rows = conn.execute(
        """SELECT f.name_canonical, f.type,
                  (SELECT COUNT(*) FROM contacts c WHERE c.firm_id=f.firm_id) AS contact_count,
                  (SELECT COUNT(*) FROM engagements e WHERE e.firm_id=f.firm_id) AS eng_count
           FROM firms f ORDER BY contact_count DESC, eng_count DESC LIMIT 15"""
    ).fetchall()
    print("\nTop 15 firms by contact count:")
    print(f"  {'FIRM':<40} {'TYPE':<16} {'CONTACTS':>9} {'ENG':>5}")
    for r in rows:
        print(f"  {r['name_canonical'][:40]:<40} {(r['type'] or '-'):<16} {r['contact_count']:>9} {r['eng_count']:>5}")
        findings.append(f"- **{r['name_canonical']}** ({r['type'] or '-'}) — {r['contact_count']} contact / {r['eng_count']} eng")

    # --- Top co-investors ---
    findings.append(header("Top 10 co-investor pairs"))
    pairs = conn.execute(
        "SELECT * FROM v_top_coinvestors LIMIT 10"
    ).fetchall()
    if pairs:
        print("\nTop 10 co-investor pairs:")
        for p in pairs:
            print(f"  {p['firm_a'][:30]:<30} <-> {p['firm_b'][:30]:<30}  shared={p['shared_company_count']}")
    else:
        print("\nNo co-investor pairs (no portfolio data yet — pending LLM enrichment).")
        findings.append("- _(empty)_ — no portfolio_companies; LLM enrichment pending.")

    # --- Review queue digest ---
    findings.append(header("Review queue (open items)"))
    rq = conn.execute(
        "SELECT category, picklist_name, COUNT(*) FROM review_queue WHERE status='open' GROUP BY 1,2 ORDER BY 3 DESC"
    ).fetchall()
    print("\nReview queue (open):")
    for r in rq:
        print(f"  {r[0]:<18} {r[1] or '-':<22} {r[2]:>4}")
        findings.append(f"- `{r[0]}` / `{r[1] or '-'}`: {r[2]}")

    # --- Append findings to ingest_log.md ---
    with open(LOG, "a") as f:
        for line in findings:
            f.write(line + "\n")

    # --- Example queries ---
    print("\n" + "=" * 78)
    print("THREE EXAMPLE QUERIES (run with sqlite3 db/investors.db)")
    print("=" * 78)
    print("""
-- 1. Filter: all VC firms we've pitched whose feedback contains "stage"
SELECT f.name_canonical, c.full_name, e.feedback
FROM firms f
JOIN engagements e ON e.firm_id = f.firm_id
LEFT JOIN contacts c ON c.contact_id = e.contact_id
WHERE f.type = 'vc'
  AND e.feedback LIKE '%stage%'
ORDER BY f.name_canonical;

-- 2. Join: which infra/energy funds passed on Faradyne, with their reason?
SELECT f.name_canonical, f.type, c.full_name, e.status, e.feedback
FROM engagements e
JOIN firms f ON f.firm_id = e.firm_id
LEFT JOIN contacts c ON c.contact_id = e.contact_id
WHERE e.sdp_client = 'Faradyne'
  AND e.status = 'passed'
  AND f.type IN ('infra_fund','vc','pe_firm')
ORDER BY length(e.feedback) DESC;

-- 3. Graph (pending LLM enrichment): top co-investors —
SELECT firm_a, firm_b, shared_company_count, shared_companies
FROM v_top_coinvestors LIMIT 15;
""")

    conn.close()


if __name__ == "__main__":
    main()
