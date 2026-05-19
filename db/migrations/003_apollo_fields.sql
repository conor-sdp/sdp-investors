-- Migration 003: Apollo-sourced enrichment fields on firms.
-- Stored separately from heuristic-scraped fields so provenance is clear.

ALTER TABLE firms ADD COLUMN apollo_id                  TEXT;
ALTER TABLE firms ADD COLUMN apollo_name                TEXT;
ALTER TABLE firms ADD COLUMN apollo_website             TEXT;
ALTER TABLE firms ADD COLUMN apollo_description         TEXT;
ALTER TABLE firms ADD COLUMN apollo_short_description   TEXT;
ALTER TABLE firms ADD COLUMN apollo_industry            TEXT;
ALTER TABLE firms ADD COLUMN apollo_industries          TEXT;   -- json
ALTER TABLE firms ADD COLUMN apollo_keywords            TEXT;   -- json
ALTER TABLE firms ADD COLUMN apollo_employee_count      INTEGER;
ALTER TABLE firms ADD COLUMN apollo_employee_range      TEXT;
ALTER TABLE firms ADD COLUMN apollo_annual_revenue      REAL;
ALTER TABLE firms ADD COLUMN apollo_founded_year        INTEGER;
ALTER TABLE firms ADD COLUMN apollo_linkedin_url        TEXT;
ALTER TABLE firms ADD COLUMN apollo_twitter_url         TEXT;
ALTER TABLE firms ADD COLUMN apollo_facebook_url        TEXT;
ALTER TABLE firms ADD COLUMN apollo_phone               TEXT;
ALTER TABLE firms ADD COLUMN apollo_hq_street           TEXT;
ALTER TABLE firms ADD COLUMN apollo_hq_city             TEXT;
ALTER TABLE firms ADD COLUMN apollo_hq_state            TEXT;
ALTER TABLE firms ADD COLUMN apollo_hq_country          TEXT;
ALTER TABLE firms ADD COLUMN apollo_total_funding       REAL;
ALTER TABLE firms ADD COLUMN apollo_latest_funding_stage TEXT;
ALTER TABLE firms ADD COLUMN apollo_status              TEXT;   -- matched | not_found | failed
ALTER TABLE firms ADD COLUMN apollo_enriched_at         TEXT;

CREATE INDEX IF NOT EXISTS idx_firms_apollo_status ON firms(apollo_status);

INSERT OR IGNORE INTO schema_migrations(version) VALUES ('003_apollo_fields');
