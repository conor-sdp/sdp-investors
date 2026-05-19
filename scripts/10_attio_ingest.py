"""
Parse persisted Attio MCP results and update firms.attio_* columns.

Looks at:
  - All /tool-results/mcp-*list-records*.txt files (overflow JSON-ish dumps)
  - All /tool-results/toolu_*.json wrapper files containing Attio responses

Extracts per company: domains, strongest_connection_strength, last_interaction,
first_interaction, team count, name, description, categories.

Matches by domain to firms in our DB. Idempotent.
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "db" / "investors.db"
TR = Path("/Users/conorwilmot/.claude/projects/-Users-conorwilmot/1274f6c2-b60d-4268-83de-97a6c0278446/tool-results")

STRENGTH_SCORE = {
    "Very weak": 1, "Weak": 2, "Good": 3, "Strong": 4, "Very strong": 5,
}


def parse_attio_records_text(text: str) -> list[dict]:
    """The persisted Attio response is YAML-ish. We just regex over it record by record."""
    records = []
    # Split on lines starting with "  - record_id:" to get each record block
    blocks = re.split(r"\n  - record_id:\s*", text)
    for b in blocks[1:]:  # skip pre-text before first record
        rec = {}
        # record_id is at the top
        m = re.match(r"\s*([0-9a-f-]+)", b)
        if m:
            rec["record_id"] = m.group(1)
        # name (singular text line)
        m = re.search(r"\n\s+name:\s*\"?([^\n\"]+)\"?", b)
        if m:
            rec["name"] = m.group(1).strip()
        # domains[N]: <value> — single-domain pattern
        m = re.search(r"\n\s+domains\[\d+\]:\s*([^\n]+)", b)
        if m:
            rec["domain"] = m.group(1).strip().lower()
        # description (often multi-line; capture up to next newline)
        m = re.search(r"\n\s+description:\s*\"([^\"]+)\"", b)
        if m:
            rec["description"] = m.group(1).strip()
        # strongest_connection_strength
        m = re.search(r"\n\s+strongest_connection_strength:\s*([A-Za-z ]+)", b)
        if m:
            rec["connection_strength"] = m.group(1).strip()
        # last_interaction (format: "HH:MM DD/MM/YYYY")
        m = re.search(r"\n\s+last_interaction:\s*\"?(\d{2}:\d{2}\s+\d{2}/\d{2}/\d{4})\"?", b)
        if m:
            rec["last_interaction_raw"] = m.group(1).strip()
        m = re.search(r"\n\s+first_interaction:\s*\"?(\d{2}:\d{2}\s+\d{2}/\d{2}/\d{4})\"?", b)
        if m:
            rec["first_interaction_raw"] = m.group(1).strip()
        # categories[N] — single line list
        m = re.search(r"\n\s+categories\[\d+\]:\s*([^\n]+)", b)
        if m:
            rec["categories"] = [c.strip() for c in m.group(1).split(",")]
        # estimated_arr_usd
        m = re.search(r"\n\s+estimated_arr_usd:\s*\"?([^\n\"]+)\"?", b)
        if m:
            rec["estimated_arr"] = m.group(1).strip()
        # team_count (count of "people," entries within team[N]{...}: block)
        team_block = re.search(r"team\[(\d+)\]", b)
        if team_block:
            rec["team_count"] = int(team_block.group(1))
        if rec.get("domain"):
            records.append(rec)
    return records


def to_iso(raw: str | None) -> str | None:
    """'14:35 25/09/2025' -> '2025-09-25T14:35:00'."""
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%H:%M %d/%m/%Y").isoformat()
    except ValueError:
        return None


def main():
    # 1) Collect candidate files from tool-results
    candidates = sorted(TR.glob("mcp-*list-records*.txt"))
    # Also wrapper json files
    wrapper = sorted(TR.glob("toolu_*.json"))
    print(f"Found {len(candidates)} overflow .txt + {len(wrapper)} toolu_*.json — scanning for Attio company data ...")

    records: list[dict] = []
    for path in candidates + wrapper:
        try:
            raw = path.read_text()
        except Exception:
            continue
        # Attio responses are recognizable by these markers
        if "strongest_connection_strength" not in raw and "primary_domain" not in raw:
            continue
        # For wrapped .json files we want the inner 'text' field unescaped
        if path.suffix == ".json":
            try:
                data = json.loads(raw)
                if isinstance(data, list) and data and "text" in data[0]:
                    raw = data[0]["text"]
            except (json.JSONDecodeError, KeyError, IndexError):
                pass
        if "record_id:" not in raw:
            continue
        records.extend(parse_attio_records_text(raw))

    print(f"Parsed {len(records)} Attio company records.")

    # 2) Deduplicate by record_id (since pages may overlap if requested twice)
    seen, deduped = set(), []
    for r in records:
        if r.get("record_id") in seen:
            continue
        seen.add(r["record_id"])
        deduped.append(r)
    print(f"After dedup: {len(deduped)} unique records.")

    # 3) Match by domain against our firms
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    firm_by_domain = {
        r["domain"]: r["firm_id"]
        for r in conn.execute("SELECT firm_id, lower(domain) AS domain FROM firms WHERE domain IS NOT NULL").fetchall()
    }
    matched = 0
    for rec in deduped:
        firm_id = firm_by_domain.get(rec["domain"])
        if not firm_id:
            continue
        conn.execute(
            """UPDATE firms SET
                 attio_id                       = ?,
                 attio_name                     = ?,
                 attio_description              = ?,
                 attio_connection_strength      = ?,
                 attio_connection_strength_score= ?,
                 attio_last_interaction         = ?,
                 attio_first_interaction        = ?,
                 attio_categories               = ?,
                 attio_estimated_arr            = ?,
                 attio_team_count               = ?,
                 attio_ingested_at              = ?
               WHERE firm_id = ?""",
            (
                rec.get("record_id"),
                rec.get("name"),
                rec.get("description"),
                rec.get("connection_strength"),
                STRENGTH_SCORE.get(rec.get("connection_strength")),
                to_iso(rec.get("last_interaction_raw")),
                to_iso(rec.get("first_interaction_raw")),
                json.dumps(rec.get("categories", [])) if rec.get("categories") else None,
                rec.get("estimated_arr"),
                rec.get("team_count"),
                datetime.now(timezone.utc).isoformat(),
                firm_id,
            ),
        )
        matched += 1
    conn.commit()

    print(f"\nMatched {matched} Attio records to existing firms.")
    print(f"({len(deduped) - matched} Attio records had no matching domain in our DB.)\n")

    # Summary
    print("=== Connection-strength distribution (matched firms) ===")
    for r in conn.execute("""
        SELECT COALESCE(attio_connection_strength,'<no attio>') AS s,
               COUNT(*) AS n
        FROM firms GROUP BY 1
        ORDER BY CASE WHEN attio_connection_strength_score IS NULL THEN -1 ELSE attio_connection_strength_score END DESC
    """).fetchall():
        print(f"  {r['s']:<20} {r['n']}")

    print("\n=== Sample: warmest investor relationships (matched in both Attio + our DB) ===")
    for r in conn.execute("""
        SELECT name_canonical, type, attio_connection_strength, attio_last_interaction
        FROM firms
        WHERE attio_id IS NOT NULL
        ORDER BY attio_connection_strength_score DESC, attio_last_interaction DESC
        LIMIT 12
    """).fetchall():
        print(f"  {r['name_canonical'][:35]:<35} {r['type'] or '-':<14} "
              f"{r['attio_connection_strength']:<12} last={r['attio_last_interaction'] or '-'}")
    conn.close()


if __name__ == "__main__":
    main()
