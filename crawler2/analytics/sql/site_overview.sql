-- Question: per site, how far did each page get through the pipeline?
-- (discovered → fetched → fetched OK → parsed → media discovered). Datasets: urls,
-- page_observations, page_extracts, page_media_refs. Run:
--   crawler2-analytics sql <export> --file crawler2/analytics/sql/site_overview.sql
WITH d AS (SELECT site, count(*) AS discovered FROM urls GROUP BY 1),
     f AS (SELECT final_site AS site, count(DISTINCT final_url_id) AS fetched,
                  count(DISTINCT final_url_id) FILTER (WHERE http_status BETWEEN 200 AND 299) AS fetched_ok
           FROM page_observations GROUP BY 1),
     p AS (SELECT site, count(DISTINCT url_id) AS parsed FROM page_extracts GROUP BY 1),
     m AS (SELECT site, count(DISTINCT locator_url) AS media_discovered FROM page_media_refs GROUP BY 1)
SELECT coalesce(site, '(unknown site)') AS site,
       coalesce(discovered, 0) AS discovered, coalesce(fetched, 0) AS fetched,
       coalesce(fetched_ok, 0) AS fetched_ok, coalesce(parsed, 0) AS parsed,
       coalesce(media_discovered, 0) AS media_discovered
FROM d FULL JOIN f USING (site) FULL JOIN p USING (site) FULL JOIN m USING (site)
ORDER BY discovered DESC, site;
