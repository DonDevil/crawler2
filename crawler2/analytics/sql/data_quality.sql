-- Question: which rows lack facts that reports rely on? (counts, not repairs)
SELECT 'urls without site' AS check_name, count(*) AS n FROM urls WHERE site IS NULL
UNION ALL SELECT 'observations in unknown date', count(*) FROM page_observations WHERE date = 'unknown'
UNION ALL SELECT 'observations without extract', count(*) FROM page_observations o
          WHERE o.page_version_id NOT IN (SELECT page_version_id FROM page_extracts)
UNION ALL SELECT 'media observations without content id', count(*) FROM media_observations
          WHERE content_id IS NULL
UNION ALL SELECT 'extracts with truncation', count(*) FROM page_extracts WHERE len(truncated) > 0;
