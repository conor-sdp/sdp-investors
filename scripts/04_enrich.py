"""
Phase 4: Enrich firms by scraping their websites.

For each firm with a domain:
  - GET https://{domain}/ (and https://www.{domain}/ as fallback)
  - Cache HTML to enrichment_cache/{firm_id}.html
  - Parse with trafilatura -> enrichment_cache/{firm_id}.json
  - Heuristic extraction (no LLM):
      strategy        : og:description / meta[name=description]
      url             : final response URL
      detected_pages  : list of nav links matching /portfolio /team /people
                        /companies /investments /about
  - Mark firms.enrichment_status: complete | failed | partial

This is a deterministic pipeline. LLM-based field extraction (Haiku) for
sectors / stages / check_size / portfolio companies is the next step and
requires ANTHROPIC_API_KEY. If the key is not set, we still produce the
HTML + parsed text cache so the LLM step can run later without re-scraping.

Rate limit: 1 request per second per host (httpx async + per-host lock).
Concurrency: 12 workers across distinct hosts.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import httpx
from selectolax.parser import HTMLParser

import sys
sys.path.insert(0, str(Path(__file__).parent))
from _lib import connect, ROOT


CACHE_DIR = ROOT / "enrichment_cache"
CACHE_DIR.mkdir(exist_ok=True)
LOG = ROOT / "ingest_log.md"

USER_AGENT = "Mozilla/5.0 (compatible; SDPInvestorNetwork/0.1; +https://standarddemand.com)"
TIMEOUT = httpx.Timeout(10.0, connect=5.0)
PER_HOST_DELAY = 1.0     # seconds between requests to the same host
CONCURRENCY = 12         # global worker count
MAX_HTML_BYTES = 2_000_000  # 2 MB hard cap

INTERESTING_PATHS = re.compile(
    r"^/(portfolio|investments|companies|team|people|about|approach|strategy|fund)s?/?$",
    re.IGNORECASE,
)


def html_cache_path(firm_id: str) -> Path:
    return CACHE_DIR / f"{firm_id}.html"


def json_cache_path(firm_id: str) -> Path:
    return CACHE_DIR / f"{firm_id}.json"


def already_cached(firm_id: str) -> bool:
    return html_cache_path(firm_id).exists() and json_cache_path(firm_id).exists()


def extract_meta(html: str) -> dict:
    """Pull title, og:description, meta description, and interesting nav links."""
    out = {"title": None, "description": None, "og_title": None, "nav_links": []}
    try:
        tree = HTMLParser(html)
    except Exception:  # noqa: BLE001
        return out
    title_node = tree.css_first("title")
    if title_node:
        out["title"] = (title_node.text() or "").strip()[:200] or None
    for node in tree.css("meta"):
        nm = (node.attributes.get("name") or "").lower()
        prop = (node.attributes.get("property") or "").lower()
        content = node.attributes.get("content") or ""
        if prop == "og:title" and not out["og_title"]:
            out["og_title"] = content.strip()[:200]
        if (nm == "description" or prop == "og:description") and not out["description"]:
            out["description"] = content.strip()[:600]
    # Nav links — capture distinct interesting paths
    seen = set()
    for a in tree.css("a[href]"):
        href = a.attributes.get("href") or ""
        # Normalize relative paths
        path = href.split("?", 1)[0].split("#", 1)[0]
        m = INTERESTING_PATHS.match(path)
        if m:
            cat = m.group(1).lower().rstrip("s")
            if cat not in seen:
                seen.add(cat)
                out["nav_links"].append({"category": cat, "href": href, "text": (a.text() or "").strip()[:80]})
    return out


def parsed_text(html: str) -> str | None:
    try:
        import trafilatura
        text = trafilatura.extract(html, include_comments=False, include_tables=False) or ""
        return text[:50000] or None
    except Exception:  # noqa: BLE001
        return None


async def fetch_one(client: httpx.AsyncClient, host_locks: dict[str, asyncio.Lock],
                    host_last: dict[str, float], firm_id: str, domain: str) -> dict:
    """Return {status, url, error?, bytes, extracted}."""
    if already_cached(firm_id):
        # Read from cache
        try:
            html = html_cache_path(firm_id).read_text(errors="replace")
            j = json.loads(json_cache_path(firm_id).read_text())
            return {"status": "cached", "url": j.get("final_url"), "bytes": len(html),
                    "extracted": j.get("extracted") or {}}
        except Exception as e:  # noqa: BLE001
            return {"status": "cache_error", "error": str(e)}
    host = domain
    lock = host_locks[host]
    async with lock:
        # Per-host throttle
        wait = PER_HOST_DELAY - (time.time() - host_last.get(host, 0))
        if wait > 0:
            await asyncio.sleep(wait)
        host_last[host] = time.time()
        candidates = [f"https://{domain}/", f"https://www.{domain}/", f"http://{domain}/"]
        last_err = None
        for url in candidates:
            try:
                r = await client.get(url, follow_redirects=True, timeout=TIMEOUT)
                if r.status_code >= 400:
                    last_err = f"HTTP {r.status_code} on {url}"
                    continue
                content = r.content[:MAX_HTML_BYTES]
                if not content:
                    last_err = "empty response"
                    continue
                html = content.decode(r.encoding or "utf-8", errors="replace")
                extracted = extract_meta(html)
                text = parsed_text(html)
                # Persist
                html_cache_path(firm_id).write_text(html)
                json_cache_path(firm_id).write_text(json.dumps({
                    "firm_id": firm_id,
                    "final_url": str(r.url),
                    "status_code": r.status_code,
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "extracted": extracted,
                    "parsed_text": text,
                }, default=str))
                return {"status": "ok", "url": str(r.url), "bytes": len(html), "extracted": extracted}
            except (httpx.HTTPError, httpx.TimeoutException) as e:
                last_err = f"{type(e).__name__}: {str(e)[:120]}"
                continue
        return {"status": "failed", "error": last_err or "unknown"}


async def run_enrichment(firms: list[dict]) -> dict[str, dict]:
    results: dict[str, dict] = {}
    host_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
    host_last: dict[str, float] = {}
    sem = asyncio.Semaphore(CONCURRENCY)
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT},
                                 follow_redirects=True, timeout=TIMEOUT) as client:
        async def worker(firm):
            async with sem:
                res = await fetch_one(client, host_locks, host_last, firm["firm_id"], firm["domain"])
                results[firm["firm_id"]] = res
                marker = {"ok": "✓", "cached": ".", "failed": "✗"}.get(res["status"], "?")
                print(f"  {marker} {firm['domain']:<40}  {res['status']}", flush=True)
        await asyncio.gather(*(worker(f) for f in firms))
    return results


def main():
    conn = connect()
    firms = conn.execute(
        """SELECT firm_id, name_canonical, domain, url, enrichment_status
           FROM firms
           WHERE domain IS NOT NULL
             AND enrichment_status != 'complete'
           ORDER BY name_canonical"""
    ).fetchall()
    firms = [dict(r) for r in firms]
    print(f"Eligible firms (have domain, not yet enriched): {len(firms)}\n")
    if not firms:
        print("Nothing to enrich.")
        return

    t0 = time.time()
    results = asyncio.run(run_enrichment(firms))
    elapsed = time.time() - t0

    # Persist results to firms table
    ok = failed = cached = 0
    for fid, res in results.items():
        if res["status"] in ("ok", "cached"):
            ok += 1
            if res["status"] == "cached":
                cached += 1
            ex = res.get("extracted") or {}
            strategy = ex.get("description")
            url = res.get("url")
            conn.execute(
                """UPDATE firms
                   SET url = COALESCE(?, url),
                       strategy = COALESCE(strategy, ?),
                       enrichment_status = 'complete',
                       enrichment_error = NULL,
                       last_enriched_at = ?
                   WHERE firm_id = ?""",
                (url, strategy, datetime.now(timezone.utc).isoformat(), fid),
            )
        else:
            failed += 1
            conn.execute(
                """UPDATE firms
                   SET enrichment_status = 'failed',
                       enrichment_error = ?,
                       last_enriched_at = ?
                   WHERE firm_id = ?""",
                (res.get("error", "unknown"), datetime.now(timezone.utc).isoformat(), fid),
            )
    conn.commit()

    failure_rate = failed / max(1, len(firms))
    print("\n" + "=" * 78)
    print("PHASE 4 ENRICHMENT SUMMARY")
    print("=" * 78)
    print(f"  attempted:    {len(firms)}")
    print(f"  ok:           {ok}  (of which cached: {cached})")
    print(f"  failed:       {failed}")
    print(f"  failure rate: {failure_rate:.1%}")
    print(f"  elapsed:      {elapsed:.1f}s")
    print(f"  cache dir:    {CACHE_DIR}")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print()
        print("  NOTE: ANTHROPIC_API_KEY not set.")
        print("  Phase 4 ran the *scraping + heuristic-extraction* layer only.")
        print("  LLM-based field extraction (sectors, stages, check_size, team,")
        print("  portfolio companies) is the next step. To run it later, set")
        print("  ANTHROPIC_API_KEY and re-invoke an LLM-extraction script that")
        print("  reads enrichment_cache/*.json and updates firms / portfolio_companies.")

    if failure_rate > 0.20:
        print()
        print(f"  TRIP-WIRE: failure rate {failure_rate:.1%} > 20%.")
        print(f"  This likely indicates a scraping config issue worth fixing.")
        print(f"  Examine first 10 failures:")
        rows = conn.execute(
            "SELECT name_canonical, domain, enrichment_error FROM firms "
            "WHERE enrichment_status='failed' LIMIT 10"
        ).fetchall()
        for r in rows:
            print(f"    {r['name_canonical']:<35}  {r['domain']:<30}  {r['enrichment_error'][:60]}")

    # Append to ingest_log.md
    log_entry = f"""
## Phase 4 — Enrichment ({datetime.now(timezone.utc).isoformat()})

- attempted: **{len(firms)}**
- ok: **{ok}** (cached: {cached})
- failed: **{failed}** ({failure_rate:.1%})
- elapsed: {elapsed:.1f}s
- ANTHROPIC_API_KEY: {"set" if os.environ.get("ANTHROPIC_API_KEY") else "**unset** — heuristic-only enrichment"}

Heuristic extraction populated `firms.strategy` from `og:description` /
`meta[name=description]` where present and `firms.url` from final response URL.
LLM-based extraction (sectors, stages, check_size, portfolio, team) deferred.
"""
    with open(LOG, "a") as f:
        f.write(log_entry)
    print(f"\n  Appended summary to {LOG.relative_to(ROOT.parent)}")
    conn.close()


if __name__ == "__main__":
    main()
