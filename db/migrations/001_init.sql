-- SDP Investor Network Database — initial schema
-- Migration: 001_init
-- All categorical text columns store taxonomy slugs from taxonomy.yml.
-- All json columns are stored as TEXT (validated as JSON in application code).
-- Re-runs are idempotent: every CREATE uses IF NOT EXISTS.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------
-- Schema version table (so future migrations can detect state)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_migrations (
  version       TEXT PRIMARY KEY,
  applied_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------
-- firms — canonical record per investor organization
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS firms (
  firm_id              TEXT PRIMARY KEY,         -- ulid
  name_canonical       TEXT NOT NULL,
  name_aliases         TEXT NOT NULL DEFAULT '[]', -- json array of strings
  domain               TEXT,                     -- e.g. "kkr.com", lowercased
  url                  TEXT,                     -- homepage
  type                 TEXT,                     -- firm_type slug
  aum_usd_m            REAL,
  fund_size_usd_m      REAL,
  fund_vintage         INTEGER,
  strategy             TEXT,                     -- free-text one-liner
  stages               TEXT NOT NULL DEFAULT '[]', -- json: stage_focus slugs
  sectors              TEXT NOT NULL DEFAULT '[]', -- json: sector slugs
  geographies          TEXT NOT NULL DEFAULT '[]', -- json: geography slugs
  check_size_min_usd_m REAL,
  check_size_max_usd_m REAL,
  check_size_bucket    TEXT,                     -- check_size_bucket slug (derived)
  hq_city              TEXT,
  hq_country           TEXT,                     -- geography slug
  last_enriched_at     TEXT,
  enrichment_status    TEXT NOT NULL DEFAULT 'pending',  -- pending|complete|failed|skipped
  enrichment_error     TEXT,
  created_at           TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at           TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_firms_name_canonical ON firms(name_canonical);
CREATE INDEX        IF NOT EXISTS idx_firms_domain         ON firms(domain);
CREATE INDEX        IF NOT EXISTS idx_firms_type           ON firms(type);

-- ---------------------------------------------------------------------
-- contacts — people, anchored to a firm
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS contacts (
  contact_id           TEXT PRIMARY KEY,         -- ulid
  firm_id              TEXT NOT NULL REFERENCES firms(firm_id) ON DELETE CASCADE,
  first_name           TEXT,
  last_name            TEXT,
  full_name            TEXT,                     -- canonical "First Last"
  title                TEXT,                     -- raw title string
  email                TEXT,                     -- normalized lowercase
  phone                TEXT,
  linkedin             TEXT,                     -- normalized URL
  seniority            TEXT,                     -- seniority slug
  role                 TEXT,                     -- contact_role slug
  last_contacted_at    TEXT,                     -- ISO date
  relationship_owner   TEXT,                     -- relationship_owner slug
  bio                  TEXT,
  notes                TEXT,
  created_at           TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at           TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_contacts_email
  ON contacts(email) WHERE email IS NOT NULL;
CREATE INDEX        IF NOT EXISTS idx_contacts_firm  ON contacts(firm_id);
CREATE INDEX        IF NOT EXISTS idx_contacts_name  ON contacts(full_name);

-- ---------------------------------------------------------------------
-- mandates — what a firm is actively looking for. Multiple per firm OK.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mandates (
  mandate_id           TEXT PRIMARY KEY,
  firm_id              TEXT NOT NULL REFERENCES firms(firm_id) ON DELETE CASCADE,
  description          TEXT NOT NULL,            -- one-liner
  sectors              TEXT NOT NULL DEFAULT '[]', -- json: sector slugs
  stages               TEXT NOT NULL DEFAULT '[]', -- json: stage_focus slugs
  check_size_min_usd_m REAL,
  check_size_max_usd_m REAL,
  geographies          TEXT NOT NULL DEFAULT '[]', -- json: geography slugs
  active               INTEGER NOT NULL DEFAULT 1,  -- bool 0/1
  source               TEXT NOT NULL,            -- mandate_source slug
  evidence_url         TEXT,
  as_of                TEXT,                     -- ISO date when observed
  created_at           TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_mandates_firm ON mandates(firm_id);
CREATE INDEX IF NOT EXISTS idx_mandates_active ON mandates(active);

-- ---------------------------------------------------------------------
-- engagements — every recorded touch between SDP and a contact
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS engagements (
  engagement_id        TEXT PRIMARY KEY,
  contact_id           TEXT REFERENCES contacts(contact_id) ON DELETE SET NULL,
  firm_id              TEXT NOT NULL REFERENCES firms(firm_id) ON DELETE CASCADE,
  sdp_client           TEXT,                     -- name of SDP's client we're pitching for
  mandate_pitched      TEXT,                     -- one-liner of what was pitched
  date                 TEXT,                     -- ISO date (NULL OK)
  channel              TEXT,                     -- engagement_channel slug
  status               TEXT NOT NULL,            -- outreach_status slug
  feedback             TEXT,                     -- raw feedback text
  feedback_secondary   TEXT,                     -- "Feedback 2" column
  notes                TEXT,
  followup             INTEGER,                  -- bool 0/1 — was follow-up done
  meeting_held         INTEGER,                  -- bool 0/1
  smartlead_link       TEXT,
  responded_by         TEXT,                     -- relationship_owner slug
  created_at           TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_engagements_contact ON engagements(contact_id);
CREATE INDEX IF NOT EXISTS idx_engagements_firm    ON engagements(firm_id);
CREATE INDEX IF NOT EXISTS idx_engagements_status  ON engagements(status);
CREATE INDEX IF NOT EXISTS idx_engagements_client  ON engagements(sdp_client);
CREATE INDEX IF NOT EXISTS idx_engagements_date    ON engagements(date);

-- ---------------------------------------------------------------------
-- portfolio_companies — what a firm has invested in (from website scrape)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS portfolio_companies (
  pc_id                TEXT PRIMARY KEY,
  firm_id              TEXT NOT NULL REFERENCES firms(firm_id) ON DELETE CASCADE,
  company_name         TEXT NOT NULL,
  company_name_normalized TEXT NOT NULL,         -- lowercased, suffix-stripped — for joins
  sector               TEXT,                     -- sector slug (best-effort)
  stage                TEXT,                     -- stage_focus slug (best-effort)
  source_url           TEXT,
  created_at           TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_pc_firm     ON portfolio_companies(firm_id);
CREATE INDEX IF NOT EXISTS idx_pc_norm     ON portfolio_companies(company_name_normalized);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pc_firm_norm
  ON portfolio_companies(firm_id, company_name_normalized);

-- ---------------------------------------------------------------------
-- co_investments — derived edge: two firms share 2+ portfolio companies
-- Always stored with a_firm_id < b_firm_id (string compare).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS co_investments (
  a_firm_id            TEXT NOT NULL REFERENCES firms(firm_id) ON DELETE CASCADE,
  b_firm_id            TEXT NOT NULL REFERENCES firms(firm_id) ON DELETE CASCADE,
  shared_company_count INTEGER NOT NULL,
  shared_companies     TEXT NOT NULL DEFAULT '[]', -- json array of normalized names
  last_updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (a_firm_id, b_firm_id),
  CHECK (a_firm_id < b_firm_id)
);
CREATE INDEX IF NOT EXISTS idx_coinv_a ON co_investments(a_firm_id);
CREATE INDEX IF NOT EXISTS idx_coinv_b ON co_investments(b_firm_id);
CREATE INDEX IF NOT EXISTS idx_coinv_count ON co_investments(shared_company_count DESC);

-- ---------------------------------------------------------------------
-- sources — provenance ledger. Every entity has 1+ row here.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sources (
  source_id            INTEGER PRIMARY KEY AUTOINCREMENT,
  source_file          TEXT NOT NULL,
  source_sheet         TEXT,
  source_row           INTEGER,
  entity_type          TEXT NOT NULL,            -- firm|contact|engagement|mandate|portfolio_company
  entity_id            TEXT NOT NULL,
  raw_payload          TEXT NOT NULL,            -- json: original row content
  ingested_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_sources_entity   ON sources(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_sources_file     ON sources(source_file);

-- ---------------------------------------------------------------------
-- review_queue — anything that needs a human eye:
--   - out-of-picklist values (with rapidfuzz suggestion)
--   - ambiguous dedup conflicts (same email/name, different firm)
--   - merges where multiple sources disagree on a non-trivial field
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS review_queue (
  review_id            INTEGER PRIMARY KEY AUTOINCREMENT,
  category             TEXT NOT NULL,            -- "out_of_picklist" | "dedup_conflict" | "ambiguous_merge"
  picklist_name        TEXT,                     -- e.g. "firm_type"
  raw_value            TEXT,
  suggested_value      TEXT,
  suggestion_score     REAL,                     -- 0-100 (rapidfuzz)
  context              TEXT NOT NULL,            -- json: source ref + competing candidates
  status               TEXT NOT NULL DEFAULT 'open',  -- open|resolved|wontfix
  resolved_by          TEXT,
  resolved_at          TEXT,
  created_at           TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_review_status   ON review_queue(status);
CREATE INDEX IF NOT EXISTS idx_review_category ON review_queue(category);

-- ---------------------------------------------------------------------
-- column_maps_applied — record which file-mapping versions were used
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS column_maps_applied (
  source_file          TEXT PRIMARY KEY,
  map_file             TEXT NOT NULL,
  map_hash             TEXT NOT NULL,
  applied_at           TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------
-- Convenience views — used by exports / QA
-- ---------------------------------------------------------------------
CREATE VIEW IF NOT EXISTS v_firms_enriched AS
  SELECT
    f.*,
    (SELECT COUNT(*) FROM contacts c WHERE c.firm_id = f.firm_id)            AS contact_count,
    (SELECT COUNT(*) FROM engagements e WHERE e.firm_id = f.firm_id)         AS engagement_count,
    (SELECT COUNT(*) FROM portfolio_companies p WHERE p.firm_id = f.firm_id) AS portfolio_count
  FROM firms f;

CREATE VIEW IF NOT EXISTS v_top_coinvestors AS
  SELECT
    a.name_canonical AS firm_a,
    b.name_canonical AS firm_b,
    ci.shared_company_count,
    ci.shared_companies
  FROM co_investments ci
  JOIN firms a ON a.firm_id = ci.a_firm_id
  JOIN firms b ON b.firm_id = ci.b_firm_id
  ORDER BY ci.shared_company_count DESC;

INSERT OR IGNORE INTO schema_migrations(version) VALUES ('001_init');
