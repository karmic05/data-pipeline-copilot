-- TPC-H (standard open benchmark): top-revenue customers report.
-- Schema: orders, lineitem, customer, supplier, nation, region.
-- Deliberately riddled with anti-patterns for the copilot to surface.
CREATE OR REPLACE TABLE analytics.tpch_top_customers AS
SELECT
    c.*,                                    -- SELECT * ships every customer column
    n.n_name AS nation,
    r.r_name AS region,
    SUM(l.l_extendedprice * (1 - l.l_discount)) AS revenue,
    -- correlated scalar subquery in the projection: runs once per outer row
    (
        SELECT COUNT(*)
        FROM tpch.orders o2
        WHERE o2.o_custkey = c.c_custkey
    ) AS lifetime_orders
FROM tpch.customer c,
     tpch.orders   o,                       -- comma cross join, no ON: cartesian product
     tpch.lineitem l,                        -- second comma join, no date filter on lineitem
     tpch.nation   n,
     tpch.region   r
WHERE o.o_custkey = c.c_custkey
  AND l.l_orderkey = o.o_orderkey
  AND c.c_nationkey = n.n_nationkey
  AND n.n_regionkey = r.r_regionkey
  AND c.c_mktsegment NOT IN (               -- NOT IN over a nullable subquery
        SELECT s.s_comment
        FROM tpch.supplier s
  )
  AND r.r_name LIKE '%AMERICA%'             -- leading-wildcard LIKE, full scan
GROUP BY
    c.c_custkey,
    c.c_name,
    c.c_acctbal,
    c.c_mktsegment,
    n.n_name,
    r.r_name
ORDER BY revenue DESC;
