"""
Sweep persisted MCP tool-result files into enrichment_cache/apollo/batch_*.json
in a uniform shape. Matches each persisted response to a batch index from
_batches.json by looking at the domains in the response.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "enrichment_cache" / "apollo"
TR = Path("/Users/conorwilmot/.claude/projects/-Users-conorwilmot/1274f6c2-b60d-4268-83de-97a6c0278446/tool-results")


def parse_apollo_response(path: Path) -> dict | None:
    """Return the parsed Apollo response dict from either a toolu_*.json
    (wrapped) or raw .txt overflow file. None on failure."""
    raw = path.read_text()
    # Try direct JSON first (overflow .txt files)
    try:
        data = json.loads(raw)
        # If it's a list, it's the wrapper [{type,text}] shape
        if isinstance(data, list) and data and "text" in data[0]:
            return json.loads(data[0]["text"])
        if isinstance(data, dict) and "organizations" in data:
            return data
    except (json.JSONDecodeError, KeyError, IndexError):
        pass
    return None


def main():
    batches = json.loads((CACHE / "_batches.json").read_text())
    # Build domain -> batch_index lookup
    domain_to_batch = {}
    for i, batch_doms in enumerate(batches):
        for d in batch_doms:
            domain_to_batch[d.lower()] = i

    saved = []
    skipped = []
    for path in sorted(list(TR.glob("toolu_*.json")) + list(TR.glob("mcp-*apollo*.txt"))):
        data = parse_apollo_response(path)
        if not data or "organizations" not in data:
            skipped.append(str(path))
            continue
        orgs = data.get("organizations") or []
        # Find any matching domain to identify the batch
        batch_idx = None
        for org in orgs:
            if not org:
                continue
            d = (org.get("primary_domain") or "").lower()
            if d in domain_to_batch:
                batch_idx = domain_to_batch[d]
                break
        if batch_idx is None:
            skipped.append(f"{path.name} (couldn't identify batch)")
            continue
        out_path = CACHE / f"batch_{batch_idx:02d}.json"
        out_path.write_text(json.dumps(data, indent=2))
        saved.append((batch_idx, path.name, out_path.name, len(orgs)))

    print(f"Saved {len(saved)} batch responses:")
    for i, src, dst, n in sorted(saved):
        print(f"  batch_{i:02d}  <-  {src}  ({n} orgs)")
    if skipped:
        print(f"\nSkipped {len(skipped)}:")
        for s in skipped:
            print(f"  {s}")

    # Which batches are still missing?
    have = {i for i, *_ in saved}
    missing = [i for i in range(len(batches)) if i not in have]
    print(f"\nBatches still missing: {missing}")


if __name__ == "__main__":
    main()
