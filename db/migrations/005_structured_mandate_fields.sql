-- Migration 005: Structured fields extracted from mandate/strategy text.
-- Populated by scripts/09_mandate_extract.py (rule-based; LLM step is deferred).
-- Lets us filter with `accepts_debt = 1` instead of LIKE '%debt%'.

ALTER TABLE firms ADD COLUMN accepts_debt           INTEGER;   -- bool 0/1, NULL=unknown
ALTER TABLE firms ADD COLUMN accepts_equity         INTEGER;
ALTER TABLE firms ADD COLUMN accepts_project_finance INTEGER;
ALTER TABLE firms ADD COLUMN accepts_credit         INTEGER;
ALTER TABLE firms ADD COLUMN accepts_growth         INTEGER;
ALTER TABLE firms ADD COLUMN extracted_sectors      TEXT NOT NULL DEFAULT '[]'; -- json slug list
ALTER TABLE firms ADD COLUMN extracted_geographies  TEXT NOT NULL DEFAULT '[]';
ALTER TABLE firms ADD COLUMN extracted_check_min_usd_m REAL;
ALTER TABLE firms ADD COLUMN extracted_check_max_usd_m REAL;
ALTER TABLE firms ADD COLUMN mandate_signal_score   REAL;   -- 0..1, how confident the extraction
ALTER TABLE firms ADD COLUMN mandate_extracted_at   TEXT;

CREATE INDEX IF NOT EXISTS idx_firms_accepts_debt   ON firms(accepts_debt);
CREATE INDEX IF NOT EXISTS idx_firms_accepts_equity ON firms(accepts_equity);

INSERT OR IGNORE INTO schema_migrations(version) VALUES ('005_structured_mandate_fields');
