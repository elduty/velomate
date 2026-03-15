#!/bin/bash
# Simulate the exact chart query with "Last 30 days" from now
docker exec veloai-postgres psql -U veloai -c "
SELECT
  gs AS bucket,
  COALESCE(SUM(a.distance_m) / 1000.0, 0) AS distance_km,
  string_agg(a.name, ', ') AS activities
FROM generate_series(
  date_trunc('day', now() - interval '30 days'),
  date_trunc('day', now()),
  '1 day'::interval
) gs
LEFT JOIN activities a
  ON date_trunc('day', a.date) = gs
  AND a.sport_type = 'zwift'
GROUP BY gs
ORDER BY gs
LIMIT 5;
"
