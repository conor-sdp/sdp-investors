-- Migration 006: Attio CRM integration fields on firms.
-- These tell us relationship warmth (connection_strength) and recency
-- (last_interaction) — the dimensions that scraped data + Apollo can't give us.

ALTER TABLE firms ADD COLUMN attio_id                 TEXT;
ALTER TABLE firms ADD COLUMN attio_name               TEXT;
ALTER TABLE firms ADD COLUMN attio_description        TEXT;
ALTER TABLE firms ADD COLUMN attio_connection_strength TEXT;   -- Very weak | Weak | Good | Strong | Very strong
ALTER TABLE firms ADD COLUMN attio_connection_strength_score INTEGER;  -- 1..5 numeric mapping
ALTER TABLE firms ADD COLUMN attio_last_interaction   TEXT;     -- ISO timestamp
ALTER TABLE firms ADD COLUMN attio_first_interaction  TEXT;
ALTER TABLE firms ADD COLUMN attio_categories         TEXT;     -- json array
ALTER TABLE firms ADD COLUMN attio_estimated_arr      TEXT;
ALTER TABLE firms ADD COLUMN attio_team_count         INTEGER;  -- # of associated people in Attio
ALTER TABLE firms ADD COLUMN attio_ingested_at        TEXT;

CREATE INDEX IF NOT EXISTS idx_firms_attio_id ON firms(attio_id);

INSERT OR IGNORE INTO schema_migrations(version) VALUES ('006_attio_fields');
