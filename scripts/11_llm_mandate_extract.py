"""
LLM-based mandate extraction over the cached firm-website text + Apollo
descriptions + mandate notes. Uses Claude Haiku via the Anthropic API.

For each firm:
  - Gather available context: name, type, strategy, apollo_description,
    apollo_keywords, mandate text, and the parsed body of the firm's
    homepage (from enrichment_cache/{firm_id}.json).
  - Call Haiku with a tool-use schema that FORCES a JSON response with the
    same structured fields the rules-based extractor produces.
  - The system prompt (which carries the picklist) uses prompt caching, so
    only the per-firm user message is billed at full input rate.
  - Validate every value against taxonomy.yml; quarantine anything out-of-
    picklist to review_queue.
  - Cache the raw response to enrichment_cache/llm/{firm_id}.json so re-runs
    are idempotent and free.

Cost / scale:
  - ~320 firms × ~1.5KB input × Haiku ≈ a few dollars total once.
  - Concurrency = 8 via httpx async; rate-limit-aware retries built into SDK.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml
from anthropic import AsyncAnthropic, APIError, RateLimitError
from pydantic import BaseModel, Field, ValidationError, field_validator

# Ensure _lib gets imported first so .env is loaded
sys.path.insert(0, str(Path(__file__).parent))
from _lib import ROOT, connect, queue_review  # noqa: E402

CACHE_DIR = ROOT / "enrichment_cache"
LLM_CACHE = CACHE_DIR / "llm"
LLM_CACHE.mkdir(parents=True, exist_ok=True)

# ---- taxonomy picklists (constraints for the LLM) ----
with open(ROOT / "taxonomy.yml") as f:
    TAX = yaml.safe_load(f)

SECTOR_SLUGS = sorted(TAX["sector"].keys())
GEO_SLUGS = sorted(TAX["geography"].keys())
STAGE_SLUGS = sorted(TAX["stage_focus"].keys())

MODEL = os.environ.get("CLAUDE_MODEL_HAIKU", "claude-haiku-4-5-20251001")
CONCURRENCY = 8


# ---- Tool schema we hand Claude (enforces structured output) ----
EXTRACT_MANDATE_TOOL = {
    "name": "extract_mandate",
    "description": (
        "Record what kinds of investments this firm makes, based ONLY on the "
        "provided context. Use empty arrays / null for fields you have no "
        "evidence for. Do not infer; only report what the text supports."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "accepts_debt": {
                "type": ["boolean", "null"],
                "description": "True if the firm provides debt / lending / "
                               "credit / project debt. False only if the text "
                               "explicitly rules debt out. Null if unclear.",
            },
            "accepts_equity": {
                "type": ["boolean", "null"],
                "description": "True if firm provides equity (growth, venture, "
                               "buyout, project equity).",
            },
            "accepts_project_finance": {"type": ["boolean", "null"]},
            "accepts_credit":          {"type": ["boolean", "null"]},
            "accepts_growth":          {"type": ["boolean", "null"]},
            "sectors": {
                "type": "array",
                "description": "Sectors the firm invests in. Only slugs from "
                               "the allowed list.",
                "items": {"type": "string", "enum": SECTOR_SLUGS},
            },
            "geographies": {
                "type": "array",
                "description": "Geographies they actively invest in. Only "
                               "slugs from the allowed list.",
                "items": {"type": "string", "enum": GEO_SLUGS},
            },
            "stages": {
                "type": "array",
                "description": "Stages / strategies they fund. Only slugs.",
                "items": {"type": "string", "enum": STAGE_SLUGS},
            },
            "check_size_min_usd_m": {
                "type": ["number", "null"],
                "description": "Minimum check size in millions USD. Per deal, "
                               "not fund size or AUM.",
            },
            "check_size_max_usd_m": {"type": ["number", "null"]},
            "evidence": {
                "type": "string",
                "description": "Up to 200 chars of the source phrasing that "
                               "supports the most important fields.",
            },
            "confidence": {
                "type": "number",
                "description": "0..1, your confidence in the overall extraction.",
            },
        },
        "required": ["sectors", "geographies", "stages", "confidence"],
    },
}

SYSTEM_PROMPT = f"""You are an investment-mandate extractor for a capital advisory firm.
Read the provided context about an investor firm and call the `extract_mandate`
tool with structured fields.

Rules:
1. Only report what the text supports. Do not infer or guess.
2. For categorical fields (sectors / geographies / stages), only use slugs from
   the provided enums. If something doesn't fit, omit it.
3. `check_size_min_usd_m` / `check_size_max_usd_m` must be the PER-DEAL check
   size, NOT fund size or AUM. If the text only mentions AUM/fund size, leave
   these null.
4. Set booleans True/False only when explicitly supported; otherwise leave null.
5. If the firm is clearly NOT an investor (e.g. a startup, vendor, law firm),
   set confidence=0 and leave all other fields empty/null.

Allowed sector slugs:        {", ".join(SECTOR_SLUGS)}
Allowed geography slugs:     {", ".join(GEO_SLUGS)}
Allowed stage slugs:         {", ".join(STAGE_SLUGS)}
"""


class MandateExtraction(BaseModel):
    accepts_debt: bool | None = None
    accepts_equity: bool | None = None
    accepts_project_finance: bool | None = None
    accepts_credit: bool | None = None
    accepts_growth: bool | None = None
    sectors: list[str] = Field(default_factory=list)
    geographies: list[str] = Field(default_factory=list)
    stages: list[str] = Field(default_factory=list)
    check_size_min_usd_m: float | None = None
    check_size_max_usd_m: float | None = None
    evidence: str | None = None
    confidence: float = 0.0

    @field_validator("sectors", "geographies", "stages")
    @classmethod
    def _filter_picklist(cls, v, info):
        allowed = {
            "sectors": set(SECTOR_SLUGS),
            "geographies": set(GEO_SLUGS),
            "stages": set(STAGE_SLUGS),
        }[info.field_name]
        return sorted({s for s in v if s in allowed})


# ---- Per-firm context assembly ----

def parsed_text_snippet(firm_id: str, max_chars: int = 4000) -> str:
    """Pull the parsed homepage text from the scrape cache."""
    p = CACHE_DIR / f"{firm_id}.json"
    if not p.exists():
        return ""
    try:
        data = json.loads(p.read_text())
        text = (data.get("parsed_text") or "")
        return text[:max_chars]
    except Exception:
        return ""


def build_user_prompt(row: sqlite3.Row) -> str:
    parts = [
        f"Firm name: {row['name_canonical']}",
        f"Firm type (currently tagged): {row['type'] or '(none)'}",
    ]
    if row["apollo_name"] and row["apollo_name"].lower() != (row["name_canonical"] or "").lower():
        parts.append(f"Apollo name: {row['apollo_name']}")
    if row["apollo_industry"]:
        parts.append(f"Apollo industry: {row['apollo_industry']}")
    if row["apollo_industries"]:
        parts.append(f"Apollo industries: {row['apollo_industries']}")
    if row["apollo_keywords"]:
        kw = row["apollo_keywords"]
        try:
            kw_list = json.loads(kw)
            if isinstance(kw_list, list):
                kw = ", ".join(kw_list[:30])
        except Exception:
            pass
        parts.append(f"Apollo keywords: {kw}")
    if row["apollo_description"]:
        parts.append(f"Apollo description: {row['apollo_description']}")
    if row["strategy"]:
        parts.append(f"Strategy on file: {row['strategy']}")
    if row["mandate_descriptions"]:
        parts.append(f"Mandate notes on file: {row['mandate_descriptions']}")
    snippet = parsed_text_snippet(row["firm_id"])
    if snippet:
        parts.append(f"\nHomepage text (excerpt):\n{snippet}")
    return "\n\n".join(parts)


# ---- Per-firm cache ----

def cache_path(firm_id: str) -> Path:
    return LLM_CACHE / f"{firm_id}.json"


def already_cached(firm_id: str) -> bool:
    return cache_path(firm_id).exists()


# ---- Main extract loop ----

async def extract_one(client: AsyncAnthropic, sem: asyncio.Semaphore, firm: sqlite3.Row) -> dict:
    """Returns {firm_id, status, payload?}. status in {ok|cached|skipped|error}."""
    if already_cached(firm["firm_id"]):
        try:
            wrapper = json.loads(cache_path(firm["firm_id"]).read_text())
            # The cache wraps the structured extraction under "extraction" — unwrap.
            extraction = wrapper.get("extraction") or wrapper
            return {"firm_id": firm["firm_id"], "status": "cached", "payload": extraction}
        except Exception:
            pass  # fall through and re-call

    prompt = build_user_prompt(firm)
    if not prompt.strip():
        return {"firm_id": firm["firm_id"], "status": "skipped"}

    async with sem:
        resp = None
        last_err = None
        for attempt in range(5):
            try:
                resp = await client.messages.create(
                    model=MODEL,
                    max_tokens=1024,
                    system=[
                        {"type": "text", "text": SYSTEM_PROMPT,
                         "cache_control": {"type": "ephemeral"}}
                    ],
                    tools=[EXTRACT_MANDATE_TOOL],
                    tool_choice={"type": "tool", "name": "extract_mandate"},
                    messages=[{"role": "user", "content": prompt}],
                )
                break
            except RateLimitError as e:
                # Exponential backoff: 4s, 8s, 16s, 32s, 64s
                last_err = e
                await asyncio.sleep(4 * (2 ** attempt))
            except APIError as e:
                return {"firm_id": firm["firm_id"], "status": "error", "error": str(e)}
        if resp is None:
            return {"firm_id": firm["firm_id"], "status": "rate_limited", "error": str(last_err)}

    # Pull the tool_use block
    tool_use = None
    for block in resp.content:
        if block.type == "tool_use" and block.name == "extract_mandate":
            tool_use = block.input
            break
    if not tool_use:
        return {"firm_id": firm["firm_id"], "status": "error",
                "error": "no tool_use block in response"}

    # Persist raw
    cache_path(firm["firm_id"]).write_text(json.dumps({
        "firm_id": firm["firm_id"],
        "model": MODEL,
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "usage": {
            "input_tokens": resp.usage.input_tokens,
            "output_tokens": resp.usage.output_tokens,
            "cache_creation_input_tokens": getattr(resp.usage, "cache_creation_input_tokens", 0),
            "cache_read_input_tokens": getattr(resp.usage, "cache_read_input_tokens", 0),
        },
        "extraction": tool_use,
    }, default=str, indent=2))
    return {"firm_id": firm["firm_id"], "status": "ok", "payload": tool_use}


def apply_to_db(conn: sqlite3.Connection, firm_id: str, firm_name: str, payload: dict) -> None:
    """Merge an extraction into firms columns. Quarantine bad enum values to review_queue."""
    try:
        m = MandateExtraction(**payload)
    except ValidationError as e:
        queue_review(conn, "out_of_picklist", str(payload)[:200], "llm_extraction",
                     None, 0.0, {"firm_id": firm_id, "error": str(e)})
        return

    # The pydantic field_validator filters to allowed slugs; surface anything
    # the model returned outside that to review_queue for transparency.
    extras = {
        "sectors": set(payload.get("sectors", [])) - set(SECTOR_SLUGS),
        "geographies": set(payload.get("geographies", [])) - set(GEO_SLUGS),
        "stages": set(payload.get("stages", [])) - set(STAGE_SLUGS),
    }
    for pl, extras_set in extras.items():
        for val in extras_set:
            queue_review(conn, "out_of_picklist", val, f"llm_{pl[:-1]}",
                         None, 0.0, {"firm_id": firm_id, "firm_name": firm_name})

    # Union LLM-found sectors/geographies with existing rules-based ones.
    # The LLM is sometimes more conservative than the rules; we want the
    # superset, not the LLM-only set.
    existing = conn.execute(
        "SELECT extracted_sectors, extracted_geographies FROM firms WHERE firm_id = ?",
        (firm_id,),
    ).fetchone()
    existing_sectors = set()
    existing_geos = set()
    if existing:
        try:
            existing_sectors = set(json.loads(existing[0] or "[]"))
        except (ValueError, TypeError):
            pass
        try:
            existing_geos = set(json.loads(existing[1] or "[]"))
        except (ValueError, TypeError):
            pass
    merged_sectors = sorted(existing_sectors | set(m.sectors))
    merged_geos = sorted(existing_geos | set(m.geographies))

    conn.execute(
        """UPDATE firms SET
              accepts_debt              = COALESCE(?, accepts_debt),
              accepts_equity            = COALESCE(?, accepts_equity),
              accepts_project_finance   = COALESCE(?, accepts_project_finance),
              accepts_credit            = COALESCE(?, accepts_credit),
              accepts_growth            = COALESCE(?, accepts_growth),
              extracted_sectors         = ?,
              extracted_geographies     = ?,
              extracted_check_min_usd_m = COALESCE(?, extracted_check_min_usd_m),
              extracted_check_max_usd_m = COALESCE(?, extracted_check_max_usd_m),
              mandate_signal_score      = MAX(COALESCE(mandate_signal_score, 0), ?),
              mandate_extracted_at      = ?
            WHERE firm_id = ?""",
        (
            1 if m.accepts_debt else (0 if m.accepts_debt is False else None),
            1 if m.accepts_equity else (0 if m.accepts_equity is False else None),
            1 if m.accepts_project_finance else (0 if m.accepts_project_finance is False else None),
            1 if m.accepts_credit else (0 if m.accepts_credit is False else None),
            1 if m.accepts_growth else (0 if m.accepts_growth is False else None),
            json.dumps(merged_sectors),
            json.dumps(merged_geos),
            m.check_size_min_usd_m,
            m.check_size_max_usd_m,
            m.confidence,
            datetime.now(timezone.utc).isoformat(),
            firm_id,
        ),
    )


async def main_async():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY not set. Put it in ./.env or export it.")

    conn = connect()
    rows = conn.execute(
        """SELECT firm_id, name_canonical, type, strategy, apollo_name, apollo_industry,
                  apollo_industries, apollo_keywords, apollo_description,
                  (SELECT GROUP_CONCAT(description, ' | ') FROM mandates m
                    WHERE m.firm_id = f.firm_id) AS mandate_descriptions
           FROM firms f
           ORDER BY name_canonical"""
    ).fetchall()
    print(f"Firms to extract: {len(rows)}")

    client = AsyncAnthropic()
    sem = asyncio.Semaphore(CONCURRENCY)

    # Run concurrently
    results = await asyncio.gather(*(extract_one(client, sem, r) for r in rows))
    counts = {"ok": 0, "cached": 0, "skipped": 0, "error": 0, "rate_limited": 0}
    total_in, total_out, total_cache_read = 0, 0, 0
    firm_name_by_id = {r["firm_id"]: r["name_canonical"] for r in rows}
    for res in results:
        counts[res["status"]] = counts.get(res["status"], 0) + 1
        if res["status"] in ("ok", "cached") and "payload" in res:
            apply_to_db(conn, res["firm_id"], firm_name_by_id[res["firm_id"]], res["payload"])
        if res["status"] == "ok":
            try:
                u = json.loads(cache_path(res["firm_id"]).read_text())["usage"]
                total_in += u.get("input_tokens", 0)
                total_out += u.get("output_tokens", 0)
                total_cache_read += u.get("cache_read_input_tokens", 0)
            except Exception:
                pass
    conn.commit()

    print(f"\n=== LLM mandate extraction summary ===")
    for k, v in counts.items():
        print(f"  {k:<14} {v}")
    print(f"\n  tokens: input={total_in:,}  output={total_out:,}  cache-read={total_cache_read:,}")

    # Coverage after merge
    print("\n=== Structured signal coverage (post-LLM merge) ===")
    for k in ("accepts_debt", "accepts_equity", "accepts_project_finance",
              "accepts_credit", "accepts_growth"):
        n = conn.execute(f"SELECT COUNT(*) FROM firms WHERE {k} = 1").fetchone()[0]
        print(f"  {k:<26} {n}")
    n = conn.execute("SELECT COUNT(*) FROM firms WHERE extracted_sectors != '[]'").fetchone()[0]
    print(f"  with_sector_tag             {n}")
    n = conn.execute("SELECT COUNT(*) FROM firms WHERE extracted_geographies != '[]'").fetchone()[0]
    print(f"  with_geography_tag          {n}")
    conn.close()


if __name__ == "__main__":
    asyncio.run(main_async())
