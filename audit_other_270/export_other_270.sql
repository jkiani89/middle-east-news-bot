-- =====================================================================
-- READ-ONLY EXPORT — 270-event classification audit (ISS-036)
-- =====================================================================
-- Run by the DBA, never by the agent. Do not paste credentials anywhere.
--
--   PGOPTIONS='-c default_transaction_read_only=on -c statement_timeout=300000' \
--     psql "host=<host> dbname=defaultdb user=avnadmin sslmode=verify-full sslrootcert=<ca>" \
--          -v ON_ERROR_STOP=1 -f export_other_270.sql
--
-- Before running:
--   1. Create an empty client-side directory and make the paths below point at it.
--   2. Keep the window variables as they are for this population.
--   3. Confirm the sanity block shows transaction_read_only = on.
--
-- This file contains SELECT and \copy only. \copy is client-side: the server
-- never writes a file, and no INSERT/UPDATE/DELETE/DDL exists here.
-- =====================================================================

\set ON_ERROR_STOP on
\set win_start '2026-11-19 04:30:00+00'
\set win_end   '2026-11-19 16:30:00+00'

BEGIN TRANSACTION READ ONLY;
SET LOCAL statement_timeout = '300s';

-- 0) Sanity block. transaction_read_only must be "on".
SELECT current_user,
       session_user,
       current_database() AS database,
       current_setting('transaction_read_only') AS transaction_read_only,
       current_setting('server_version') AS server_version,
       current_setting('server_version_num')::int AS server_version_num;

-- 0b) Population sanity: the number of distinct events in the window.
SELECT count(DISTINCT e.id) AS events_in_window
  FROM events e
 WHERE EXISTS (
   SELECT 1
     FROM article_events ae
     JOIN articles a ON a.id = ae.article_id
    WHERE ae.event_id = e.id
      AND a.collected_at >= :'win_start'::timestamptz
      AND a.collected_at <  :'win_end'::timestamptz
 );

-- 1) Events in the window (superset: the audit population is derived locally).
\copy (SELECT e.id, e.title, e.event_type, e.countries::text AS countries, e.actors::text AS actors, e.location, e.importance, e.status, e.first_seen_at FROM events e WHERE EXISTS (SELECT 1 FROM article_events ae JOIN articles a ON a.id = ae.article_id WHERE ae.event_id = e.id AND a.collected_at >= :'win_start'::timestamptz AND a.collected_at < :'win_end'::timestamptz) ORDER BY e.id) TO '/tmp/audit_bundle/events.csv' WITH (FORMAT csv, HEADER true);

-- 2) Every article collected in the window, with its source name.
\copy (SELECT a.id, a.title, s.name AS source_name, a.original_url, a.published_at, a.collected_at, a.status, a.original_text FROM articles a LEFT JOIN sources s ON s.id = a.source_id WHERE a.collected_at >= :'win_start'::timestamptz AND a.collected_at < :'win_end'::timestamptz ORDER BY a.id) TO '/tmp/audit_bundle/articles.csv' WITH (FORMAT csv, HEADER true);

-- 3) Article-to-event links for those articles.
\copy (SELECT ae.article_id, ae.event_id FROM article_events ae JOIN articles a ON a.id = ae.article_id WHERE a.collected_at >= :'win_start'::timestamptz AND a.collected_at < :'win_end'::timestamptz ORDER BY ae.event_id, ae.article_id) TO '/tmp/audit_bundle/article_events.csv' WITH (FORMAT csv, HEADER true);

-- 4) Summaries for the events in the window.
\copy (SELECT sm.event_id, sm.headline_fa, sm.verification_status, sm.model_name FROM summaries sm WHERE EXISTS (SELECT 1 FROM article_events ae JOIN articles a ON a.id = ae.article_id WHERE ae.event_id = sm.event_id AND a.collected_at >= :'win_start'::timestamptz AND a.collected_at < :'win_end'::timestamptz) ORDER BY sm.event_id) TO '/tmp/audit_bundle/summaries.csv' WITH (FORMAT csv, HEADER true);

-- 5) Export counts for the bundle manifest.
SELECT (SELECT count(*) FROM events e WHERE EXISTS (
          SELECT 1 FROM article_events ae JOIN articles a ON a.id = ae.article_id
           WHERE ae.event_id = e.id
             AND a.collected_at >= :'win_start'::timestamptz
             AND a.collected_at <  :'win_end'::timestamptz)) AS events_rows,
       (SELECT count(*) FROM articles
         WHERE collected_at >= :'win_start'::timestamptz
           AND collected_at <  :'win_end'::timestamptz) AS articles_rows;

-- Read-only transaction: nothing to keep.
ROLLBACK;
