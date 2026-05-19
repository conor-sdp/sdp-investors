-- Migration 002: master relationships view
-- One row per (firm × contact). Firms with zero contacts still get one row
-- (contact fields NULL). Engagement = the most recent engagement for that
-- (firm, contact) pair. Mandates are aggregated to firm grain.
--
-- This is the primary view for proposal-building: pull a slice on
-- firm_type / sectors / check_size, and you have name + email + title +
-- last feedback + mandate, all in one row.

DROP VIEW IF EXISTS v_relationships;

CREATE VIEW v_relationships AS
WITH firm_eng AS (
  SELECT firm_id, COUNT(*) AS total_engagements
  FROM engagements GROUP BY firm_id
),
firm_mandates AS (
  SELECT firm_id,
         COUNT(*) AS mandate_count,
         GROUP_CONCAT(description, ' | ') AS mandate_descriptions,
         MIN(check_size_min_usd_m) AS mandate_check_size_min,
         MAX(check_size_max_usd_m) AS mandate_check_size_max
  FROM mandates GROUP BY firm_id
),
latest_eng AS (
  -- Most-recent engagement per (firm, contact). NULL contact_id is its own bucket.
  SELECT *
  FROM (
    SELECT e.*,
           ROW_NUMBER() OVER (
             PARTITION BY e.firm_id, COALESCE(e.contact_id, '__null__')
             ORDER BY COALESCE(e.date, '0000-00-00') DESC,
                      e.created_at DESC
           ) AS rn
    FROM engagements e
  ) WHERE rn = 1
)
SELECT
  f.firm_id || '::' || COALESCE(c.contact_id, 'firm_only') AS relationship_id,
  f.firm_id,
  f.name_canonical                       AS firm,
  f.type                                 AS firm_type,
  f.domain,
  f.url,
  f.hq_city,
  f.hq_country,
  f.sectors                              AS firm_sectors,
  f.stages                               AS firm_stages,
  f.check_size_bucket,
  f.check_size_min_usd_m                 AS firm_check_size_min_usd_m,
  f.check_size_max_usd_m                 AS firm_check_size_max_usd_m,
  f.aum_usd_m,
  f.fund_size_usd_m,
  f.strategy                             AS firm_strategy,
  f.enrichment_status,
  f.name_aliases                         AS firm_aliases,
  c.contact_id,
  c.full_name                            AS contact_name,
  c.title                                AS contact_title,
  c.seniority                            AS contact_seniority,
  c.email                                AS contact_email,
  c.linkedin                             AS contact_linkedin,
  c.relationship_owner,
  c.bio                                  AS contact_bio,
  le.date                                AS last_engagement_date,
  le.status                              AS last_engagement_status,
  le.sdp_client                          AS last_sdp_client,
  le.channel                             AS last_engagement_channel,
  le.feedback                            AS last_feedback,
  le.feedback_secondary                  AS last_feedback_2,
  le.notes                               AS last_notes,
  le.responded_by                        AS last_responded_by,
  le.smartlead_link                      AS last_smartlead_link,
  COALESCE(fe.total_engagements, 0)      AS total_engagements_for_firm,
  COALESCE(fm.mandate_count, 0)          AS mandate_count,
  fm.mandate_descriptions,
  fm.mandate_check_size_min              AS mandate_check_size_min_usd_m,
  fm.mandate_check_size_max              AS mandate_check_size_max_usd_m
FROM firms f
LEFT JOIN contacts c ON c.firm_id = f.firm_id
LEFT JOIN latest_eng le
       ON le.firm_id = f.firm_id
      AND le.contact_id IS c.contact_id      -- 'IS' handles NULL=NULL
LEFT JOIN firm_eng fe      ON fe.firm_id = f.firm_id
LEFT JOIN firm_mandates fm ON fm.firm_id = f.firm_id;

INSERT OR IGNORE INTO schema_migrations(version) VALUES ('002_v_relationships');
