-- Phase Y-11: agreement between the cheap lexical prefilter and the model's own
-- relevance verdict. Read-only; run it by hand against production.
--
-- The prefilter only records a verdict in ai_payload['prefilter']; it never rejects.
-- A row with would_reject = true AND is_relevant = true is a false negative that
-- would have been lost had the prefilter been enforced, so that cell must stay at
-- zero (or be understood term by term) before the check is allowed to gate anything.

WITH judged AS (
    SELECT
        (ai_payload -> 'prefilter' ->> 'would_reject')::boolean AS would_reject,
        (ai_payload ->> 'is_relevant')::boolean               AS is_relevant
    FROM articles
    WHERE ai_payload ? 'prefilter'
      AND ai_payload ? 'is_relevant'
)
SELECT
    would_reject,
    is_relevant,
    count(*) AS articles
FROM judged
GROUP BY would_reject, is_relevant
ORDER BY would_reject, is_relevant;

-- False-negative rate of the prefilter (worst case for enabling it):
--
-- WITH judged AS (
--     SELECT (ai_payload -> 'prefilter' ->> 'would_reject')::boolean AS would_reject,
--            (ai_payload ->> 'is_relevant')::boolean               AS is_relevant
--     FROM articles
--     WHERE ai_payload ? 'prefilter' AND ai_payload ? 'is_relevant'
-- )
-- SELECT count(*) FILTER (WHERE would_reject AND is_relevant)                   AS false_negatives,
--        count(*) FILTER (WHERE would_reject)                                   AS would_reject_rows,
--        round(
--            100.0 * count(*) FILTER (WHERE would_reject AND is_relevant)
--            / nullif(count(*) FILTER (WHERE would_reject), 0), 2
--        )                                                                      AS false_negative_pct
-- FROM judged;
