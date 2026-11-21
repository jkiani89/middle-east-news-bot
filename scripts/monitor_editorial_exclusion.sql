-- Editorial newsworthiness gate (RC-17) — read-only monitoring.
--
-- Counts, for the last 24 hours, how many analysed articles the gate rejected and
-- why (sport / tourism / incident). The reason is written by the pipeline into
-- articles.ai_payload->>'editorial_exclusion' at analysis time; a NULL there means
-- the gate did not fire, so no filter on it is needed to see the kept articles.
--
-- Usage (read-only account, e.g. the audit_select role):
--   psql "$DATABASE_URL" -f scripts/monitor_editorial_exclusion.sql
--
-- The 24-hour window uses collected_at because it is indexed
-- (ix_articles_collected_at); processing follows collection within the freshness
-- window, so this is also the window the analysed payloads were produced in.

\echo '--- Rejected by reason (last 24h) ---'
SELECT
    ai_payload->>'editorial_exclusion' AS reason,
    count(*)                           AS articles
FROM articles
WHERE collected_at >= now() - interval '24 hours'
  AND ai_payload->>'editorial_exclusion' IS NOT NULL
GROUP BY 1
ORDER BY articles DESC, reason;

\echo '--- Gate share of the analysed queue (last 24h) ---'
SELECT
    count(*) FILTER (WHERE ai_payload ? 'editorial_exclusion')                      AS gated,
    count(*) FILTER (WHERE (ai_payload->>'editorial_exclusion') IS NOT NULL)        AS suppressed,
    count(*) FILTER (WHERE ai_payload ? 'topic_key' AND ai_payload->>'topic_key' IS NOT NULL) AS published_to_a_topic,
    count(*)                                                                        AS analysed,
    round(
        100.0 * count(*) FILTER (WHERE (ai_payload->>'editorial_exclusion') IS NOT NULL)
        / nullif(count(*) FILTER (WHERE ai_payload ? 'editorial_exclusion'), 0),
        1
    )                                                                               AS suppressed_pct
FROM articles
WHERE collected_at >= now() - interval '24 hours'
  AND ai_payload IS NOT NULL;
