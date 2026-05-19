-- Migration 008: PitchBook enrichment fields.
-- Stored separately from Apollo and Attio fields so provenance is clear.

ALTER TABLE firms ADD COLUMN pb_name                     TEXT;
ALTER TABLE firms ADD COLUMN pb_description              TEXT;
ALTER TABLE firms ADD COLUMN pb_aum_usd_m                REAL;
ALTER TABLE firms ADD COLUMN pb_total_investments        INTEGER;
ALTER TABLE firms ADD COLUMN pb_active_portfolio         INTEGER;
ALTER TABLE firms ADD COLUMN pb_investor_type            TEXT;
ALTER TABLE firms ADD COLUMN pb_hq_location              TEXT;
ALTER TABLE firms ADD COLUMN pb_preferred_industries     TEXT;
ALTER TABLE firms ADD COLUMN pb_preferred_investment_types TEXT;
ALTER TABLE firms ADD COLUMN pb_website                  TEXT;
ALTER TABLE firms ADD COLUMN pb_primary_contact_name     TEXT;
ALTER TABLE firms ADD COLUMN pb_primary_contact_title    TEXT;
ALTER TABLE firms ADD COLUMN pb_primary_contact_email    TEXT;
ALTER TABLE firms ADD COLUMN pb_primary_contact_phone    TEXT;
ALTER TABLE firms ADD COLUMN pb_check_size_min_usd_m     REAL;
ALTER TABLE firms ADD COLUMN pb_check_size_max_usd_m     REAL;
ALTER TABLE firms ADD COLUMN pb_provenance               TEXT;   -- 'enrichment'|'pitchbook_only'
ALTER TABLE firms ADD COLUMN pb_ingested_at              TEXT;

CREATE INDEX IF NOT EXISTS idx_firms_pb_investor_type ON firms(pb_investor_type);

INSERT OR IGNORE INTO schema_migrations(version) VALUES ('008_pitchbook_fields');
