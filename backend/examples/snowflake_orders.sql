-- Snowflake: rebuild the enriched orders mart for analytics.
-- Deliberately riddled with anti-patterns for the copilot to surface.
CREATE OR REPLACE TABLE analytics.enriched_orders AS
SELECT
    o.*,                            -- SELECT * ships every order column
    c.customer_email,               -- PII reaching the output table
    c.customer_phone,               -- PII reaching the output table
    -- correlated scalar subquery in the projection: runs once per outer row
    (
        SELECT COUNT(*)
        FROM analytics.order_items oi
        WHERE oi.order_id = o.order_id
    ) AS item_count
FROM analytics.orders o,
     analytics.customers c          -- comma cross join, no ON: cartesian product
WHERE o.order_ts >= '2023-01-01'    -- hardcoded date, no partition pruning
  AND c.customer_email LIKE '%@gmail.com'   -- leading wildcard, full scan
  AND o.customer_id NOT IN (                -- NOT IN on a nullable column
        SELECT r.customer_id
        FROM analytics.refunds r
  );
