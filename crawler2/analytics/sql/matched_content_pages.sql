-- Question: on which pages was the content of each reported match seen?
-- Registry co-occurrence (media_observations.content_id), not an attribution decision;
-- verdict/confidence are the matcher's, unmodified. Datasets: matches, media_observations,
-- targets.
SELECT m.target_id, t.title, m.match_id, m.verdict, m.confidence,
       mo.page_site, mo.page_url, strftime(mo.observed_at, '%Y-%m-%dT%H:%M:%SZ') AS observed_at
FROM matches m
LEFT JOIN media_observations mo ON mo.content_id = m.content_id
LEFT JOIN targets t ON t.target_id = m.target_id
ORDER BY m.target_id, m.match_id, mo.page_site, mo.page_url;
