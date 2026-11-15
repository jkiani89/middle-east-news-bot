-- Phase Y-1: how the deterministic number check behaves on production data.
--
-- One row per (mode, status, unmatched count) so the false-positive rate stays
-- measurable while the feature runs in "log" mode:
--   false_positive_rate = mismatch / (clean + mismatch)
--
-- Run against the application database (read-only):
--   psql "$DATABASE_URL" -f docs/sql/number_check_stats.sql

-- 1. Overall distribution.
SELECT ai_payload->'number_check'->>'mode'                     AS mode,
       ai_payload->'number_check'->>'status'                   AS status,
       COALESCE(jsonb_array_length(ai_payload->'number_check'->'unmatched'), 0) AS unmatched,
       count(*)                                                AS articles
FROM articles
WHERE ai_payload ? 'number_check'
GROUP BY 1, 2, 3
ORDER BY articles DESC;

-- 2. Per reason_code counts (scale_mismatch / jalali_calendar / not_in_source ...).
SELECT rc.key                             AS reason_code,
       sum(rc.value::int)                 AS occurrences,
       count(DISTINCT articles.id)        AS articles
FROM articles,
     LATERAL jsonb_each_text(articles.ai_payload->'number_check'->'reason_codes') AS rc
WHERE articles.ai_payload ? 'number_check'
GROUP BY 1
ORDER BY occurrences DESC;

-- 3. Which articles were parked by the check (only meaningful in "flag" mode).
SELECT id,
       status,
       ai_payload->'number_check'->'unmatched' AS unmatched,
       collected_at
FROM articles
WHERE ai_payload->'number_check'->>'mode' = 'flag'
  AND ai_payload->'number_check'->>'blocking' = 'true'
ORDER BY collected_at DESC
LIMIT 50;
