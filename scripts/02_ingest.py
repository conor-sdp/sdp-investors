"""
Phase 2: Ingest in-scope XLSX sheets into staging tables.

In-scope sources (decided in Phase 0 review):
  - Interest sheets from 3Flash, Cirrus, Faradyne (+ HDC variant)
  - SDP MASTER Book of Business :: Master
  - Infra partners.xlsx (no headers)
  - US Family Offices 2026.xlsx (pre-enriched)

For each row:
  - normalize against taxonomy.yml (out-of-picklist => review_queue)
  - write a row to the appropriate staging_* table
  - write a source row linking back to (file, sheet, row)
  - validate with pydantic before insert

Idempotent: truncates staging_* on entry. column_maps_applied tracks which
mapping version we used per source file.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, EmailStr, field_validator

import sys
sys.path.insert(0, str(Path(__file__).parent))
from _lib import (
    DB, ROOT, SRC, connect, new_ulid, norm_email, norm_firm_name, norm_url,
    extract_domain, parse_location, parse_check_size, check_size_bucket,
    title_to_seniority, split_name, read_sheet, to_iso_date, to_bool,
    resolve_and_log, queue_review, picklist,
)


# ---------------------------------------------------------------------
# Column maps — hardcoded per source. Persisted to column_maps/ for record.
# ---------------------------------------------------------------------

# Variant A: canonical Interest sheet (3Flash, HDC, basically Cirrus)
MAP_INTEREST = {
    "header_row": 2,
    "firm_col":         "Firm",
    "contact_name_col": "Name",
    "email_col":        "Email",
    "title_col":        "Position",
    "stage_col":        "Stage",
    "firm_type_col":    "Firm Type",
    "who_replied_col":  "Who Replied",
    "followed_up_col":  "Followed Up",
    "feedback_col":     "Feedback",
    "location_col":     "Location",
    "email_ctx_col":    "Email For Context",
    "bio_col":          "Bio",
    "smartlead_col":    "Smartlead Reply Link",
    "notes_col":        "Notes",
    "feedback2_col":    "Feedback 2",
    "date_col":         "Date Replied",
}

# Variant B: Cirrus uses "Meeting held" instead of "Followed Up"
MAP_INTEREST_CIRRUS = {**MAP_INTEREST, "meeting_held_col": "Meeting held", "followed_up_col": None}

# Variant C: Faradyne split Name -> Column 3 (first) / Column 4 (last) + has Website
MAP_INTEREST_FARADYNE = {
    "header_row": 2,
    "firm_col":         "Firm",
    "contact_first_col": "Name",      # despite the name, this column holds FIRST names
    "contact_last_col":  "Column 3",  # last names
    # Column 4 is mostly empty
    "email_col":        "Email",
    "title_col":        "Position",
    "stage_col":        "Stage",
    "firm_type_col":    "Firm Type",
    "who_replied_col":  "Who Replied",
    "followed_up_col":  "Followed Up",
    "website_col":      "Website",
    "feedback_col":     "Feedback",
    "location_col":     "Location",
    "bio_col":          "Bio",
    "notes_col":        "Notes",
    "feedback2_col":    "Feedback 2",
}

# Variant D: SDP MASTER :: Master
MAP_MASTER = {
    "header_row": 1,
    "entity_type_col":  "Entity Type",
    "category_col":     "Category",
    "contact_name_col": "Name",
    "email_col":        "Contact",
    "firm_col":         "Company",
    "industry_col":     "Industry Interest",
    "deals_col":        "Deals",
    "activity_col":     "Recent activity?",
    "misc_col":         "misc",
}

# Variant E: Infra partners — no header row
MAP_INFRA = {
    "header_row": 0,
    "firm_col":         "col_0",
    "contact_name_col": "col_1",
    "email_col":        "col_2",
    "mandate_type_col": "col_3",
    # col_4 mostly empty
    "mandate_desc_col": "col_5",
}

# Variant F: US Family Offices — firm-level, pre-enriched
MAP_FAMILY_OFFICES = {
    "header_row": 1,
    "firm_col":     "Title",
    "linkedin_col": "LinkedIn",
    "url_col":      "Website",
    "strategy_col": "Description",
    "industry_col": "Industry Focus",
    "stage_col":    "Stage Focus",
    "location_col": "Location",
}

SOURCES = [
    # (file, sheet, sdp_client_label, map, variant_id)
    ("3Flash SDP Client Interest Sheet (2).xlsx", "Interest", "3Flash", MAP_INTEREST, "interest_canonical"),
    ("3Flash SDP Client Interest Sheet (2).xlsx", "HDC",      "3Flash (HDC)", MAP_INTEREST, "interest_canonical"),
    ("Cirrus v2 SDP Client Interest Sheet.xlsx", "Interest",  "Cirrus", MAP_INTEREST_CIRRUS, "interest_cirrus"),
    ("Faradyne SDP Client Interest Sheet.xlsx",  "Interest",  "Faradyne", MAP_INTEREST_FARADYNE, "interest_faradyne"),
    ("SDP MASTER Book of Business.xlsx", "Master", None, MAP_MASTER, "master_book"),
    ("Infra partners.xlsx", None, None, MAP_INFRA, "infra_partners"),
    ("US Family Offices 2026 - Location, Industry Focus, Stage Focus (1).xlsx", None, None,
     MAP_FAMILY_OFFICES, "family_offices_2026"),
]


# ---------------------------------------------------------------------
# Pydantic validators
# ---------------------------------------------------------------------
class StagingFirm(BaseModel):
    staging_id: str
    name_raw: str
    name_normalized: str
    domain: str | None = None
    url: str | None = None
    type: str | None = None       # firm_type slug or None
    hq_city: str | None = None
    hq_country: str | None = None # geography slug
    sectors: list[str] = Field(default_factory=list)
    stages: list[str] = Field(default_factory=list)
    strategy: str | None = None
    enrichment_status: str = "pending"
    source_file: str
    source_sheet: str | None = None
    source_row: int


class StagingContact(BaseModel):
    staging_id: str
    firm_name_raw: str        # link back to staging firm via name
    first_name: str | None = None
    last_name: str | None = None
    full_name: str | None = None
    title: str | None = None
    email: str | None = None
    linkedin: str | None = None
    seniority: str | None = None
    bio: str | None = None
    notes: str | None = None
    relationship_owner: str | None = None
    source_file: str
    source_sheet: str | None = None
    source_row: int

    @field_validator("email")
    @classmethod
    def _validate_email(cls, v):
        if v is None:
            return v
        if "@" not in v or "." not in v:
            return None
        return v


class StagingEngagement(BaseModel):
    staging_id: str
    firm_name_raw: str
    contact_email: str | None = None
    contact_name: str | None = None
    sdp_client: str | None = None
    date: str | None = None
    status: str | None = None       # outreach_status slug
    channel: str | None = None
    feedback: str | None = None
    feedback_secondary: str | None = None
    notes: str | None = None
    followup: int | None = None
    meeting_held: int | None = None
    smartlead_link: str | None = None
    responded_by: str | None = None
    source_file: str
    source_sheet: str | None = None
    source_row: int


class StagingMandate(BaseModel):
    staging_id: str
    firm_name_raw: str
    description: str
    sectors: list[str] = Field(default_factory=list)
    stages: list[str] = Field(default_factory=list)
    check_size_min_usd_m: float | None = None
    check_size_max_usd_m: float | None = None
    source: str = "source_sheet"
    source_file: str
    source_sheet: str | None = None
    source_row: int


# ---------------------------------------------------------------------
# Staging schema (created here, not in migration — these are intermediate)
# ---------------------------------------------------------------------
STAGING_SQL = """
DROP TABLE IF EXISTS staging_firms;
CREATE TABLE staging_firms (
  staging_id        TEXT PRIMARY KEY,
  name_raw          TEXT NOT NULL,
  name_normalized   TEXT NOT NULL,
  domain            TEXT,
  url               TEXT,
  type              TEXT,
  hq_city           TEXT,
  hq_country        TEXT,
  sectors           TEXT NOT NULL DEFAULT '[]',
  stages            TEXT NOT NULL DEFAULT '[]',
  strategy          TEXT,
  enrichment_status TEXT NOT NULL DEFAULT 'pending',
  source_file       TEXT NOT NULL,
  source_sheet      TEXT,
  source_row        INTEGER NOT NULL,
  raw_payload       TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_sfirms_norm   ON staging_firms(name_normalized);
CREATE INDEX idx_sfirms_domain ON staging_firms(domain);

DROP TABLE IF EXISTS staging_contacts;
CREATE TABLE staging_contacts (
  staging_id        TEXT PRIMARY KEY,
  firm_name_raw     TEXT NOT NULL,
  first_name        TEXT,
  last_name         TEXT,
  full_name         TEXT,
  title             TEXT,
  email             TEXT,
  linkedin          TEXT,
  seniority         TEXT,
  bio               TEXT,
  notes             TEXT,
  relationship_owner TEXT,
  source_file       TEXT NOT NULL,
  source_sheet      TEXT,
  source_row        INTEGER NOT NULL,
  raw_payload       TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_scontacts_email ON staging_contacts(email);
CREATE INDEX idx_scontacts_firm  ON staging_contacts(firm_name_raw);

DROP TABLE IF EXISTS staging_engagements;
CREATE TABLE staging_engagements (
  staging_id        TEXT PRIMARY KEY,
  firm_name_raw     TEXT NOT NULL,
  contact_email     TEXT,
  contact_name      TEXT,
  sdp_client        TEXT,
  date              TEXT,
  status            TEXT,
  channel           TEXT,
  feedback          TEXT,
  feedback_secondary TEXT,
  notes             TEXT,
  followup          INTEGER,
  meeting_held      INTEGER,
  smartlead_link    TEXT,
  responded_by      TEXT,
  source_file       TEXT NOT NULL,
  source_sheet      TEXT,
  source_row        INTEGER NOT NULL,
  raw_payload       TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_sengagements_firm ON staging_engagements(firm_name_raw);

DROP TABLE IF EXISTS staging_mandates;
CREATE TABLE staging_mandates (
  staging_id        TEXT PRIMARY KEY,
  firm_name_raw     TEXT NOT NULL,
  description       TEXT NOT NULL,
  sectors           TEXT NOT NULL DEFAULT '[]',
  stages            TEXT NOT NULL DEFAULT '[]',
  check_size_min_usd_m REAL,
  check_size_max_usd_m REAL,
  source            TEXT NOT NULL,
  source_file       TEXT NOT NULL,
  source_sheet      TEXT,
  source_row        INTEGER NOT NULL,
  raw_payload       TEXT NOT NULL DEFAULT '{}'
);
"""


def get(row: dict, key: str | None) -> Any:
    if not key:
        return None
    v = row.get(key)
    if isinstance(v, str):
        v = v.strip()
        if not v:
            return None
    return v


def extract_sectors_from_text(text: str | None, conn, source_file: str,
                              source_sheet: str | None, source_row: int) -> list[str]:
    """Pull sector slugs from a free-text 'Industry Interest' / 'Industry Focus' field."""
    if not text:
        return []
    out = set()
    # Try comma-split items first
    parts = [p.strip() for p in str(text).replace("/", ",").replace(";", ",").split(",")]
    for p in parts:
        if not p:
            continue
        slug, score = resolve_and_log_(conn, "sector", p, source_file, source_sheet, source_row,
                                       label="industry_interest")
        if slug:
            out.add(slug)
    return sorted(out)


def resolve_and_log_(conn, picklist_name, raw, source_file, source_sheet, source_row, label=""):
    slug = resolve_and_log(conn, picklist_name, raw, source_file, source_sheet, source_row, label)
    return slug, None


# ---------------------------------------------------------------------
# Per-source ingest functions
# ---------------------------------------------------------------------

def ingest_interest(conn, file: str, sheet: str, sdp_client: str, m: dict, log: list):
    path = SRC / file
    headers, data = read_sheet(path, sheet, header_row=m["header_row"])
    inserted = {"firms": 0, "contacts": 0, "engagements": 0}
    for i, row in enumerate(data, start=m["header_row"] + 1):
        firm_raw = get(row, m.get("firm_col"))
        if not firm_raw:
            continue
        firm_name = str(firm_raw).strip()
        firm_norm = norm_firm_name(firm_name)
        # ---- Firm ----
        firm_type_raw = get(row, m.get("firm_type_col"))
        firm_type_slug = resolve_and_log(conn, "firm_type", firm_type_raw, file, sheet, i, "firm_type")
        loc_raw = get(row, m.get("location_col"))
        hq_city, hq_country = parse_location(loc_raw)
        email = norm_email(get(row, m.get("email_col")))
        domain = extract_domain(email) or extract_domain(get(row, m.get("website_col")))
        url = norm_url(get(row, m.get("website_col")))

        sfirm_id = new_ulid()
        firm_payload = {
            "firm_raw": firm_name, "firm_type_raw": firm_type_raw, "location_raw": loc_raw,
            "email_for_domain": email, "website": url,
        }
        sfirm = StagingFirm(
            staging_id=sfirm_id, name_raw=firm_name, name_normalized=firm_norm,
            domain=domain, url=url, type=firm_type_slug,
            hq_city=hq_city, hq_country=hq_country,
            source_file=file, source_sheet=sheet, source_row=i,
        )
        conn.execute(
            """INSERT INTO staging_firms (staging_id, name_raw, name_normalized, domain, url, type,
                  hq_city, hq_country, sectors, stages, strategy, enrichment_status,
                  source_file, source_sheet, source_row, raw_payload)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (sfirm.staging_id, sfirm.name_raw, sfirm.name_normalized, sfirm.domain, sfirm.url,
             sfirm.type, sfirm.hq_city, sfirm.hq_country, json.dumps(sfirm.sectors),
             json.dumps(sfirm.stages), sfirm.strategy, sfirm.enrichment_status,
             sfirm.source_file, sfirm.source_sheet, sfirm.source_row, json.dumps(firm_payload, default=str)),
        )
        inserted["firms"] += 1

        # ---- Contact ----
        if m.get("contact_first_col"):
            first = get(row, m["contact_first_col"])
            last = get(row, m["contact_last_col"])
            full = " ".join([p for p in [first, last] if p])
        else:
            full = get(row, m.get("contact_name_col"))
            first, last, full = split_name(full) if full else (None, None, None)

        title = get(row, m.get("title_col"))
        bio = get(row, m.get("bio_col"))
        who_replied = get(row, m.get("who_replied_col"))
        ro_slug = resolve_and_log(conn, "relationship_owner", who_replied, file, sheet, i, "who_replied")

        contact_inserted = False
        if email or full:
            sc_id = new_ulid()
            seniority_slug = title_to_seniority(title)
            contact_payload = {"name_raw": full, "title_raw": title, "email_raw": email,
                               "who_replied_raw": who_replied, "bio_raw": bio}
            sc = StagingContact(
                staging_id=sc_id, firm_name_raw=firm_norm,
                first_name=first, last_name=last, full_name=full or None,
                title=title, email=email, seniority=seniority_slug,
                bio=bio, relationship_owner=ro_slug,
                source_file=file, source_sheet=sheet, source_row=i,
            )
            conn.execute(
                """INSERT INTO staging_contacts (staging_id, firm_name_raw, first_name, last_name,
                      full_name, title, email, linkedin, seniority, bio, notes, relationship_owner,
                      source_file, source_sheet, source_row, raw_payload)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (sc.staging_id, sc.firm_name_raw, sc.first_name, sc.last_name, sc.full_name,
                 sc.title, sc.email, sc.linkedin, sc.seniority, sc.bio, sc.notes, sc.relationship_owner,
                 sc.source_file, sc.source_sheet, sc.source_row, json.dumps(contact_payload, default=str)),
            )
            inserted["contacts"] += 1
            contact_inserted = True

        # ---- Engagement ----
        stage_raw = get(row, m.get("stage_col"))
        feedback = get(row, m.get("feedback_col"))
        notes = get(row, m.get("notes_col"))
        # if there's any signal of contact (stage/feedback/notes/smartlead) emit engagement
        if any([stage_raw, feedback, notes, get(row, m.get("smartlead_col"))]):
            status_slug = resolve_and_log(conn, "outreach_status", stage_raw, file, sheet, i, "outreach_stage")
            channel_slug = "smartlead" if get(row, m.get("smartlead_col")) else "email"
            se_id = new_ulid()
            eng_payload = {"stage_raw": stage_raw, "feedback_raw": feedback,
                           "smartlead_raw": get(row, m.get("smartlead_col"))}
            se = StagingEngagement(
                staging_id=se_id, firm_name_raw=firm_norm,
                contact_email=email, contact_name=full,
                sdp_client=sdp_client, date=to_iso_date(get(row, m.get("date_col"))),
                status=status_slug, channel=channel_slug,
                feedback=feedback if isinstance(feedback, str) else None,
                feedback_secondary=get(row, m.get("feedback2_col")),
                notes=notes if isinstance(notes, str) else None,
                followup=to_bool(get(row, m.get("followed_up_col"))),
                meeting_held=to_bool(get(row, m.get("meeting_held_col"))),
                smartlead_link=get(row, m.get("smartlead_col")) if isinstance(get(row, m.get("smartlead_col")), str) else None,
                responded_by=ro_slug,
                source_file=file, source_sheet=sheet, source_row=i,
            )
            conn.execute(
                """INSERT INTO staging_engagements (staging_id, firm_name_raw, contact_email, contact_name,
                      sdp_client, date, status, channel, feedback, feedback_secondary, notes,
                      followup, meeting_held, smartlead_link, responded_by,
                      source_file, source_sheet, source_row, raw_payload)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (se.staging_id, se.firm_name_raw, se.contact_email, se.contact_name,
                 se.sdp_client, se.date, se.status, se.channel, se.feedback, se.feedback_secondary,
                 se.notes, se.followup, se.meeting_held, se.smartlead_link, se.responded_by,
                 se.source_file, se.source_sheet, se.source_row, json.dumps(eng_payload, default=str)),
            )
            inserted["engagements"] += 1
    log.append({"source": f"{file}::{sheet}", "inserted": inserted})


def ingest_master(conn, file: str, sheet: str, m: dict, log: list):
    path = SRC / file
    headers, data = read_sheet(path, sheet, header_row=m["header_row"])
    inserted = {"firms": 0, "contacts": 0, "engagements": 0}
    for i, row in enumerate(data, start=m["header_row"] + 1):
        # In Master, "Company" is the firm; "Name" is the person; "Contact" is the email.
        firm_raw = get(row, m["firm_col"])
        contact_name = get(row, m["contact_name_col"])
        entity_type_raw = get(row, m["entity_type_col"])

        # Some Master rows have no firm but a Name + Entity Type — treat the person AS the firm
        # if Entity Type is "Individual".
        if not firm_raw and contact_name and entity_type_raw == "Individual":
            firm_raw = contact_name

        if not firm_raw:
            continue

        firm_name = str(firm_raw).strip()
        firm_norm = norm_firm_name(firm_name)
        email = norm_email(get(row, m["email_col"]))
        domain = extract_domain(email)
        firm_type_slug = resolve_and_log(conn, "firm_type", entity_type_raw, file, sheet, i, "entity_type")

        industry_raw = get(row, m["industry_col"])
        sectors = extract_sectors_from_text(industry_raw, conn, file, sheet, i)
        # Check size hint sometimes baked into industry_raw ("$3M Check size")
        cmin, cmax = parse_check_size(industry_raw)

        sfirm_id = new_ulid()
        firm_payload = {"firm_raw": firm_name, "entity_type_raw": entity_type_raw,
                        "category_raw": get(row, m["category_col"]),
                        "industry_raw": industry_raw,
                        "deals_raw": get(row, m["deals_col"]),
                        "activity_raw": get(row, m["activity_col"]),
                        "misc_raw": get(row, m["misc_col"])}
        sf = StagingFirm(
            staging_id=sfirm_id, name_raw=firm_name, name_normalized=firm_norm,
            domain=domain, type=firm_type_slug, sectors=sectors,
            source_file=file, source_sheet=sheet, source_row=i,
        )
        conn.execute(
            """INSERT INTO staging_firms (staging_id, name_raw, name_normalized, domain, url, type,
                  hq_city, hq_country, sectors, stages, strategy, enrichment_status,
                  source_file, source_sheet, source_row, raw_payload)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (sf.staging_id, sf.name_raw, sf.name_normalized, sf.domain, sf.url, sf.type,
             sf.hq_city, sf.hq_country, json.dumps(sf.sectors), json.dumps(sf.stages),
             sf.strategy, sf.enrichment_status,
             sf.source_file, sf.source_sheet, sf.source_row, json.dumps(firm_payload, default=str)),
        )
        inserted["firms"] += 1

        # If there's a check-size hint, emit a mandate row
        if cmin is not None or cmax is not None or sectors:
            sm_id = new_ulid()
            conn.execute(
                """INSERT INTO staging_mandates (staging_id, firm_name_raw, description, sectors, stages,
                      check_size_min_usd_m, check_size_max_usd_m, source,
                      source_file, source_sheet, source_row, raw_payload)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (sm_id, firm_norm, industry_raw or "Inferred from Master book",
                 json.dumps(sectors), json.dumps([]),
                 cmin, cmax, "source_sheet", file, sheet, i,
                 json.dumps({"industry_raw": industry_raw}, default=str)),
            )

        # Contact (only if we have a name and it's not the same as firm name)
        if contact_name and contact_name != firm_raw:
            first, last, full = split_name(contact_name)
            sc_id = new_ulid()
            conn.execute(
                """INSERT INTO staging_contacts (staging_id, firm_name_raw, first_name, last_name,
                      full_name, title, email, linkedin, seniority, bio, notes, relationship_owner,
                      source_file, source_sheet, source_row, raw_payload)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (sc_id, firm_norm, first, last, full, None, email, None, None, None,
                 get(row, m["activity_col"]) or get(row, m["misc_col"]),
                 None, file, sheet, i,
                 json.dumps({"name_raw": contact_name, "email_raw": email}, default=str)),
            )
            inserted["contacts"] += 1

        # Recent activity? emits an engagement row
        activity = get(row, m["activity_col"])
        deals = get(row, m["deals_col"])
        if activity or deals:
            se_id = new_ulid()
            conn.execute(
                """INSERT INTO staging_engagements (staging_id, firm_name_raw, contact_email, contact_name,
                      sdp_client, date, status, channel, feedback, feedback_secondary, notes,
                      followup, meeting_held, smartlead_link, responded_by,
                      source_file, source_sheet, source_row, raw_payload)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (se_id, firm_norm, email, contact_name, None, None, None, "other",
                 None, None,
                 " | ".join([str(x) for x in [deals, activity] if x]) or None,
                 None, None, None, None,
                 file, sheet, i,
                 json.dumps({"deals_raw": deals, "activity_raw": activity}, default=str)),
            )
            inserted["engagements"] += 1
    log.append({"source": f"{file}::{sheet}", "inserted": inserted})


def ingest_infra(conn, file: str, m: dict, log: list):
    path = SRC / file
    headers, data = read_sheet(path, None, header_row=m["header_row"])
    inserted = {"firms": 0, "contacts": 0, "mandates": 0}
    for i, row in enumerate(data, start=1):
        firm_raw = get(row, m["firm_col"])
        if not firm_raw:
            continue
        firm_name = str(firm_raw).strip()
        firm_norm = norm_firm_name(firm_name)
        email = norm_email(get(row, m["email_col"]))
        domain = extract_domain(email)

        sf_id = new_ulid()
        firm_payload = {"firm_raw": firm_name, "type_raw": get(row, m["mandate_type_col"]),
                        "desc_raw": get(row, m["mandate_desc_col"])}
        # All entries in Infra partners are infra-related; default firm_type to infra_fund
        # unless email domain suggests something else
        conn.execute(
            """INSERT INTO staging_firms (staging_id, name_raw, name_normalized, domain, url, type,
                  hq_city, hq_country, sectors, stages, strategy, enrichment_status,
                  source_file, source_sheet, source_row, raw_payload)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (sf_id, firm_name, firm_norm, domain, None, "infra_fund",
             None, None,
             json.dumps(["infrastructure", "energy_transition"]),
             json.dumps([]),
             get(row, m["mandate_desc_col"]),
             "pending",
             file, "Sheet1", i, json.dumps(firm_payload, default=str)),
        )
        inserted["firms"] += 1

        # Contact
        contact_name = get(row, m["contact_name_col"])
        if contact_name or email:
            first, last, full = split_name(contact_name) if contact_name else (None, None, None)
            sc_id = new_ulid()
            conn.execute(
                """INSERT INTO staging_contacts (staging_id, firm_name_raw, first_name, last_name,
                      full_name, title, email, linkedin, seniority, bio, notes, relationship_owner,
                      source_file, source_sheet, source_row, raw_payload)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (sc_id, firm_norm, first, last, full, None, email, None, None, None, None, None,
                 file, "Sheet1", i, json.dumps({"name_raw": contact_name}, default=str)),
            )
            inserted["contacts"] += 1

        # Mandate
        mandate_desc = get(row, m["mandate_desc_col"])
        mandate_type = get(row, m["mandate_type_col"])
        cmin, cmax = parse_check_size(mandate_desc)
        stage_slug, _ = (None, None)
        if mandate_type:
            s, _ = (resolve_and_log(conn, "stage_focus", mandate_type, file, "Sheet1", i, "mandate_type"), None)
            stage_slug = s
        stages_list = [stage_slug] if stage_slug else []
        if mandate_desc:
            sm_id = new_ulid()
            conn.execute(
                """INSERT INTO staging_mandates (staging_id, firm_name_raw, description, sectors, stages,
                      check_size_min_usd_m, check_size_max_usd_m, source,
                      source_file, source_sheet, source_row, raw_payload)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (sm_id, firm_norm, mandate_desc,
                 json.dumps(["infrastructure", "energy_transition"]),
                 json.dumps(stages_list),
                 cmin, cmax, "source_sheet",
                 file, "Sheet1", i,
                 json.dumps({"type_raw": mandate_type, "desc_raw": mandate_desc}, default=str)),
            )
            inserted["mandates"] += 1
    log.append({"source": f"{file}", "inserted": inserted})


def ingest_family_offices(conn, file: str, m: dict, log: list):
    path = SRC / file
    headers, data = read_sheet(path, None, header_row=m["header_row"])
    inserted = {"firms": 0}
    for i, row in enumerate(data, start=m["header_row"] + 1):
        firm_raw = get(row, m["firm_col"])
        if not firm_raw:
            continue
        firm_name = str(firm_raw).strip()
        firm_norm = norm_firm_name(firm_name)
        url = norm_url(get(row, m["url_col"]))
        # "N/A" url => treat as null
        if url and "n/a" in url.lower():
            url = None
        domain = extract_domain(url) if url else None
        loc_raw = get(row, m["location_col"])
        hq_city, hq_country = parse_location(loc_raw)
        industry_raw = get(row, m["industry_col"])
        sectors = []
        if industry_raw:
            slug = resolve_and_log(conn, "sector", industry_raw, file, "US Family Offices", i, "industry_focus")
            if slug:
                sectors.append(slug)
        stage_raw = get(row, m["stage_col"])
        stages = []
        if stage_raw:
            slug = resolve_and_log(conn, "stage_focus", stage_raw, file, "US Family Offices", i, "stage_focus")
            if slug:
                stages.append(slug)
        strategy = get(row, m["strategy_col"])
        # Truncate over-long descriptions to ~600 chars
        if strategy and len(strategy) > 600:
            strategy = strategy[:597] + "..."
        sf_id = new_ulid()
        firm_payload = {"title_raw": firm_name, "url_raw": get(row, m["url_col"]),
                        "industry_raw": industry_raw, "stage_raw": stage_raw,
                        "location_raw": loc_raw, "linkedin_raw": get(row, m["linkedin_col"])}
        conn.execute(
            """INSERT INTO staging_firms (staging_id, name_raw, name_normalized, domain, url, type,
                  hq_city, hq_country, sectors, stages, strategy, enrichment_status,
                  source_file, source_sheet, source_row, raw_payload)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (sf_id, firm_name, firm_norm, domain, url, "family_office",
             hq_city, hq_country, json.dumps(sectors), json.dumps(stages),
             strategy, "complete",  # already enriched
             file, "US Family Offices", i, json.dumps(firm_payload, default=str)),
        )
        inserted["firms"] += 1
    log.append({"source": f"{file}", "inserted": inserted})


def write_column_map(file: str, variant_id: str, m: dict):
    out = ROOT / "column_maps" / f"{Path(file).stem}__{variant_id}.json"
    out.write_text(json.dumps({"variant_id": variant_id, "map": m}, indent=2))
    return out


def main():
    print(f"DB: {DB}")
    print(f"Source dir: {SRC}\n")
    conn = connect()
    print("Creating staging tables ...")
    conn.executescript(STAGING_SQL)
    conn.commit()

    log = []
    for file, sheet, sdp_client, m, variant_id in SOURCES:
        print(f"\nIngesting {file} :: {sheet or '(first)'}  [{variant_id}]")
        map_path = write_column_map(file, variant_id, m)
        # record column_maps_applied
        map_hash = hashlib.md5(json.dumps(m, sort_keys=True, default=str).encode()).hexdigest()[:12]
        conn.execute(
            "INSERT OR REPLACE INTO column_maps_applied(source_file, map_file, map_hash) VALUES (?,?,?)",
            (f"{file}::{sheet or ''}", str(map_path.relative_to(ROOT.parent)), map_hash),
        )
        if variant_id in ("interest_canonical", "interest_cirrus", "interest_faradyne"):
            ingest_interest(conn, file, sheet, sdp_client, m, log)
        elif variant_id == "master_book":
            ingest_master(conn, file, sheet, m, log)
        elif variant_id == "infra_partners":
            ingest_infra(conn, file, m, log)
        elif variant_id == "family_offices_2026":
            ingest_family_offices(conn, file, m, log)
        conn.commit()

    # ---- Summary ----
    print("\n" + "=" * 78)
    print("PHASE 2 INGEST SUMMARY")
    print("=" * 78)
    totals = {"firms": 0, "contacts": 0, "engagements": 0, "mandates": 0}
    for entry in log:
        print(f"  {entry['source']}")
        for k, v in entry["inserted"].items():
            print(f"    {k:<14} {v:>5}")
            totals[k] = totals.get(k, 0) + v
    print("\nTOTALS:")
    for k, v in totals.items():
        print(f"  staging_{k:<12} {v:>5}")

    # Review queue summary
    rq_count = conn.execute("SELECT COUNT(*) FROM review_queue WHERE status='open'").fetchone()[0]
    print(f"\nReview queue (open): {rq_count}")
    if rq_count > 0:
        cats = conn.execute("SELECT category, picklist_name, COUNT(*) c FROM review_queue WHERE status='open' GROUP BY 1,2 ORDER BY 3 DESC").fetchall()
        for r in cats:
            print(f"  {r['category']:<20} {r['picklist_name'] or '-':<22} {r['c']:>4}")

    conn.close()


if __name__ == "__main__":
    main()
