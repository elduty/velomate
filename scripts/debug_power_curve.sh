#!/bin/bash
# Test power duration curve query — replace 5 with an activity ID that has power data
ACTIVITY_ID=${1:-5}
docker exec veloai-postgres psql -U veloai -c "
WITH w AS (
  SELECT
    AVG(power) OVER (ORDER BY time_offset RANGE BETWEEN 4 PRECEDING AND CURRENT ROW) AS p5,
    AVG(power) OVER (ORDER BY time_offset RANGE BETWEEN 29 PRECEDING AND CURRENT ROW) AS p30,
    AVG(power) OVER (ORDER BY time_offset RANGE BETWEEN 59 PRECEDING AND CURRENT ROW) AS p60,
    AVG(power) OVER (ORDER BY time_offset RANGE BETWEEN 299 PRECEDING AND CURRENT ROW) AS p300,
    AVG(power) OVER (ORDER BY time_offset RANGE BETWEEN 1199 PRECEDING AND CURRENT ROW) AS p1200
  FROM activity_streams
  WHERE activity_id = $ACTIVITY_ID AND power IS NOT NULL AND power > 0
)
SELECT 5 AS secs, ROUND(MAX(p5)::numeric, 0) AS best_power FROM w
UNION ALL SELECT 30, ROUND(MAX(p30)::numeric, 0) FROM w
UNION ALL SELECT 60, ROUND(MAX(p60)::numeric, 0) FROM w
UNION ALL SELECT 300, ROUND(MAX(p300)::numeric, 0) FROM w
UNION ALL SELECT 1200, ROUND(MAX(p1200)::numeric, 0) FROM w
ORDER BY secs;
"
