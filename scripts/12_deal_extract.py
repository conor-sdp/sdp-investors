"""
Deal-criteria extractor. Takes text from a deck / transcript / memo and
returns structured criteria suitable for feeding into best_contacts.py.

Used by the Streamlit app (app.py). Can also run standalone:
    .venv/bin/python scripts/12_deal_extract.py < some.txt
    .venv/bin/python scripts/12_deal_extract.py --file deck.pdf

Note: we deliberately do NOT use `from __future__ import annotations` here.
The Streamlit app loads this module via importlib.spec_from_file_location,
which means pydantic v2's lazy forward-reference resolution can't find the
module globals when validating. Keeping real type objects (not strings)
sidesteps the issue cleanly.
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

import yaml
from anthropic import Anthropic
from pydantic import BaseModel, Field, field_validator

# Trigger .env load
sys.path.insert(0, str(Path(__file__).parent))
from _lib import ROOT  # noqa: E402

with open(ROOT / "taxonomy.yml") as f:
    TAX = yaml.safe_load(f)

SECTOR_SLUGS = sorted(TAX["sector"].keys())
GEO_SLUGS = sorted(TAX["geography"].keys())
STAGE_SLUGS = sorted(TAX["stage_focus"].keys())

MODEL = os.environ.get("CLAUDE_MODEL_HAIKU", "claude-haiku-4-5-20251001")

EXTRACT_DEAL_TOOL = {
    "name": "extract_deal",
    "description": (
        "Read the deal materials (pitch deck, memo, or call transcript) and "
        "extract the structured criteria needed to find matching investors. "
        "Only report what's clearly supported by the text."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "company_name": {
                "type": ["string", "null"],
                "description": "Company seeking capital, exactly as it appears.",
            },
            "one_line_summary": {
                "type": "string",
                "description": "One sentence (<200 chars) describing what the "
                               "company does + the financing need.",
            },
            "capital_type": {
                "type": "string",
                "enum": ["debt", "equity", "both", "unknown"],
                "description": "Type of capital being raised. 'debt' includes "
                               "project finance, senior debt, mezzanine, credit. "
                               "'equity' includes growth, venture, project equity. "
                               "Use 'both' if explicitly raising both layers.",
            },
            "primary_sector": {
                "type": "string",
                "enum": SECTOR_SLUGS,
                "description": "The single primary sector slug. If unclear, use 'other'.",
            },
            "secondary_sectors": {
                "type": "array",
                "items": {"type": "string", "enum": SECTOR_SLUGS},
                "description": "Up to 3 additional sector tags relevant to the deal.",
            },
            "geographies": {
                "type": "array",
                "items": {"type": "string", "enum": GEO_SLUGS},
                "description": "Where the project / company is located or where "
                               "the capital will be deployed. Pick from picklist.",
            },
            "stage": {
                "type": ["string", "null"],
                "enum": STAGE_SLUGS + [None],
                "description": "Investment stage if mentioned (project_debt, "
                               "project_equity, growth, series_b, etc.). Null if unclear.",
            },
            "check_size_min_usd_m": {
                "type": ["number", "null"],
                "description": "Minimum capital sought (millions USD).",
            },
            "check_size_max_usd_m": {
                "type": ["number", "null"],
                "description": "Maximum capital sought (millions USD).",
            },
            "key_terms": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Up to 6 short phrases (<60 chars) lifted from the "
                               "deal that should match investor mandates "
                               "(e.g., 'operating solar asset', 'C&I rooftop', "
                               "'tax equity bridge').",
            },
            "confidence": {
                "type": "number",
                "description": "0..1 confidence in the overall extraction.",
            },
        },
        "required": ["one_line_summary", "capital_type", "primary_sector",
                     "geographies", "confidence"],
    },
}

SYSTEM_PROMPT = f"""You read deal materials (pitch decks, memos, or call transcripts)
for Standard Demand Partners, a capital advisory firm. Extract the structured
criteria needed to find matching investors.

Rules:
1. Read carefully. Only report fields the text clearly supports.
2. For categorical fields, use only the provided enum slugs.
3. `capital_type` is the most important field — be precise:
   - "debt" if seeking loans, credit, senior debt, mezzanine, project finance
   - "equity" if raising growth/venture/project equity
   - "both" if explicitly raising both tranches
   - "unknown" if the materials don't make it clear
4. `check_size_min/max_usd_m` should be the capital being RAISED, not company revenue or AUM.
5. `primary_sector` must be a single slug. Use 'other' only when no slug fits.
6. Set confidence high (0.8+) only when the materials clearly state the deal type and sector.

Allowed sector slugs:    {", ".join(SECTOR_SLUGS)}
Allowed geography slugs: {", ".join(GEO_SLUGS)}
Allowed stage slugs:     {", ".join(STAGE_SLUGS)}
"""


class DealCriteria(BaseModel):
    company_name: Optional[str] = None
    one_line_summary: str
    capital_type: str
    primary_sector: str
    secondary_sectors: list[str] = Field(default_factory=list)
    geographies: list[str] = Field(default_factory=list)
    stage: Optional[str] = None
    check_size_min_usd_m: Optional[float] = None
    check_size_max_usd_m: Optional[float] = None
    key_terms: list[str] = Field(default_factory=list)
    confidence: float = 0.0

    @field_validator("primary_sector")
    @classmethod
    def _validate_primary_sector(cls, v):
        return v if v in SECTOR_SLUGS else "other"

    @field_validator("secondary_sectors", "geographies", "key_terms")
    @classmethod
    def _filter_lists(cls, v, info):
        if info.field_name == "secondary_sectors":
            return [s for s in v if s in SECTOR_SLUGS][:5]
        if info.field_name == "geographies":
            return [g for g in v if g in GEO_SLUGS][:5]
        return [str(s)[:80] for s in v][:8]


def extract_text_from_pdf(data: bytes) -> str:
    from io import BytesIO
    from pypdf import PdfReader
    reader = PdfReader(BytesIO(data))
    return "\n\n".join(page.extract_text() or "" for page in reader.pages)


def extract_text_from_docx(data: bytes) -> str:
    from io import BytesIO
    from docx import Document
    doc = Document(BytesIO(data))
    return "\n".join(p.text for p in doc.paragraphs if p.text.strip())


def extract_text_from_upload(filename: str, data: bytes) -> str:
    name = filename.lower()
    if name.endswith(".pdf"):
        return extract_text_from_pdf(data)
    if name.endswith(".docx"):
        return extract_text_from_docx(data)
    if name.endswith((".txt", ".md")):
        return data.decode("utf-8", errors="replace")
    raise ValueError(f"Unsupported file type: {filename}. Use PDF, DOCX, or paste text.")


def extract_deal_criteria(text: str, max_text_chars: int = 12000) -> DealCriteria:
    """Call Haiku to extract structured criteria. Caps input length to keep cost
    bounded — most decks compress fine to 12K chars."""
    if not text or not text.strip():
        raise ValueError("Empty deck text — nothing to extract.")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY not set. Configure it in ./.env.")
    truncated = text[:max_text_chars]
    client = Anthropic()
    resp = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        tools=[EXTRACT_DEAL_TOOL],
        tool_choice={"type": "tool", "name": "extract_deal"},
        messages=[{"role": "user", "content": f"Deal materials below:\n\n{truncated}"}],
    )
    tool_use = None
    for block in resp.content:
        if block.type == "tool_use" and block.name == "extract_deal":
            tool_use = block.input
            break
    if not tool_use:
        raise RuntimeError("No tool_use block in Claude response.")
    return DealCriteria(**tool_use)


def main_cli():
    p = argparse.ArgumentParser()
    p.add_argument("--file", type=str, default=None,
                   help="Path to a deck/memo file (PDF, DOCX, TXT). If omitted, reads from stdin.")
    args = p.parse_args()

    if args.file:
        data = Path(args.file).read_bytes()
        text = extract_text_from_upload(args.file, data)
    else:
        text = sys.stdin.read()

    criteria = extract_deal_criteria(text)
    print(json.dumps(criteria.model_dump(), indent=2, default=str))


if __name__ == "__main__":
    main_cli()
