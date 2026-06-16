-- NYC TLC Trip Records (open data): daily yellow-taxi trip metrics.
-- Real schema columns: vendor_id, tpep_pickup_datetime, tpep_dropoff_datetime,
-- passenger_count, trip_distance, fare_amount, tip_amount, total_amount,
-- payment_type, pulocationid, dolocationid.
-- Deliberately riddled with anti-patterns for the copilot to surface.
CREATE OR REPLACE TABLE analytics.nyc_taxi_daily_metrics AS
SELECT
    *,                                              -- SELECT * on a huge full-table scan
    AVG(fare_amount)   AS avg_fare,
    AVG(tip_amount)    AS avg_tip,
    SUM(total_amount)  AS gross_revenue,
    COUNT(*)           AS trips
FROM nyc.yellow_tripdata
-- function-wrapped filter column defeats partition pruning + zone maps:
WHERE DATE(tpep_pickup_datetime) = '2024-03-15'    -- hardcoded date, function on column
  AND passenger_count > 0
  AND trip_distance   > 0
  AND payment_type    IN (1, 2)
GROUP BY
    vendor_id,
    pulocationid,
    dolocationid,
    payment_type
ORDER BY gross_revenue DESC;
