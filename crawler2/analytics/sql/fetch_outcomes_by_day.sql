-- Question: how did fetch outcomes and capabilities evolve per UTC day?
-- Dataset: fetch_attempts (date = UTC day of finished_at).
SELECT date, capability, outcome, count(*) AS attempts,
       count(DISTINCT site) AS sites, round(avg(bytes_received)) AS avg_bytes
FROM fetch_attempts
GROUP BY ALL
ORDER BY date, capability, attempts DESC;
