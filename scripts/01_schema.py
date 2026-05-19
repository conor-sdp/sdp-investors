"""Apply pending SQL migrations to db/investors.db. Idempotent."""
from __future__ import annotations
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "db" / "investors.db"
MIGS = ROOT / "db" / "migrations"

def main():
    DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB)
    conn.executescript("PRAGMA foreign_keys = ON;")
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')))")
    cur.execute("SELECT version FROM schema_migrations")
    applied = {r[0] for r in cur.fetchall()}
    for path in sorted(MIGS.glob("*.sql")):
        ver = path.stem
        if ver in applied:
            print(f"  [skip] {ver} (already applied)")
            continue
        print(f"  [apply] {ver}")
        sql = path.read_text()
        conn.executescript(sql)
        conn.commit()
    cur.execute("SELECT version, applied_at FROM schema_migrations ORDER BY version")
    print("\nApplied migrations:")
    for v, a in cur.fetchall():
        print(f"  {v}  ({a})")
    conn.close()
    print(f"\nDB at {DB.relative_to(ROOT.parent)}")

if __name__ == "__main__":
    main()
