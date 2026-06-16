/**
 * Sample pipelines shown in the editor's sample picker.
 * CONTRACT: keep the `Sample` shape and the `samples` / `DEFAULT_SAMPLE`
 * exports stable — lib/store.tsx and the editor panel depend on them.
 *
 * Every sample is deliberately written to exhibit classic, realistic
 * data-engineering anti-patterns so the analyzer has plenty to flag.
 */

export interface Sample {
  id: string;
  label: string;
  format: string;
  language: "sql" | "python" | "yaml";
  code: string;
}

export const samples: Sample[] = [
  {
    id: "snowflake-orders",
    label: "Snowflake — order enrichment (intentionally bad)",
    format: "sql",
    language: "sql",
    code: `-- Daily order enrichment job (Snowflake)
-- Rebuilds the full enriched_orders table every run.
CREATE OR REPLACE TABLE analytics.enriched_orders AS
SELECT
    o.order_id,
    o.created_at,
    c.customer_email,            -- PII flows straight to the output
    c.customer_phone,            -- PII flows straight to the output
    p.amount,
    p.currency,
    a.address_text,
    (
        SELECT AVG(p2.amount)    -- correlated scalar subquery in projection
        FROM raw.payments p2
        WHERE p2.customer_id = c.customer_id
    ) AS avg_customer_spend
FROM raw.orders o,
     raw.customers c             -- comma + cross join, no ON condition
JOIN raw.payments p   ON p.order_id = o.order_id
LEFT JOIN raw.addresses a
       ON a.address_text LIKE '%' || c.city   -- leading-wildcard LIKE
WHERE o.status NOT IN (
        SELECT status FROM raw.excluded_statuses  -- NOT IN over nullable col
      )
  AND o.created_at > '2024-01-01'              -- hardcoded date, no partition filter
ORDER BY o.created_at DESC;
`,
  },
  {
    id: "airflow-etl",
    label: "Airflow — daily ETL DAG (intentionally bad)",
    format: "airflow",
    language: "python",
    code: `from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.providers.amazon.aws.sensors.s3 import S3KeySensor
from airflow.providers.apache.spark.operators.spark_submit import (
    SparkSubmitOperator,
)

default_args = {
    "retries": 0,            # no retries on transient failures
}

dag = DAG(
    dag_id="daily_revenue_etl",
    schedule="@daily",
    start_date=datetime(2024, 1, 1),
    default_args=default_args,
    # catchup never set -> defaults to True, will backfill on deploy
    # no SLA, no owner, no on_failure_callback
)

wait_for_drop = S3KeySensor(
    task_id="wait_for_drop",
    bucket_key="s3://landing/revenue/{{ ds }}/_SUCCESS",
    mode="poke",             # blocks a worker slot the whole time
    poke_interval=30,
    timeout=60 * 60 * 12,
    dag=dag,
)

def _extract(**context):
    import pandas as pd
    df = pd.read_parquet("s3://landing/revenue/")
    # pushing a whole DataFrame through XCom
    context["ti"].xcom_push(key="rows", value=df.to_dict())

extract = PythonOperator(task_id="extract", python_callable=_extract, dag=dag)

transform_1 = SparkSubmitOperator(task_id="transform_1", application="jobs/t1.py", dag=dag)
transform_2 = SparkSubmitOperator(task_id="transform_2", application="jobs/t2.py", dag=dag)
transform_3 = SparkSubmitOperator(task_id="transform_3", application="jobs/t3.py", dag=dag)

def _load(**context):
    rows = context["ti"].xcom_pull(key="rows")
    print("loading", len(rows))

load = PythonOperator(task_id="load", python_callable=_load, dag=dag)

# strict sequential chain of heavy jobs, no parallelism
wait_for_drop >> extract >> transform_1 >> transform_2 >> transform_3 >> load
`,
  },
  {
    id: "dbt-incremental",
    label: "dbt — incremental model + schema.yml (intentionally bad)",
    format: "dbt",
    language: "sql",
    code: `{{
    config(
        materialized='incremental',
        unique_key='event_id'
    )
}}

-- No incremental_strategy and no is_incremental() guard:
-- every run re-scans the entire source.
SELECT
    e.event_id,
    e.user_id,
    u.customer_email,        -- PII column passed through untested
    e.event_type,
    e.payload,
    e.event_ts
FROM {{ source('raw', 'events') }} e
LEFT JOIN {{ ref('dim_users') }} u
       ON u.user_id = e.user_id

--- schema.yml
version: 2

sources:
  - name: raw
    tables:
      - name: events          # zero tests, no description

models:
  - name: fct_events
    columns:
      - name: event_id        # no not_null / unique test
      - name: customer_email  # PII, no description, no test
`,
  },
  {
    id: "spark-streaming",
    label: "Spark — structured streaming job (intentionally bad)",
    format: "spark",
    language: "python",
    code: `from pyspark.sql import SparkSession
from pyspark.sql import functions as F

spark = SparkSession.builder.appName("clickstream").getOrCreate()

events = (
    spark.readStream
    .format("kafka")
    .option("kafka.bootstrap.servers", "broker:9092")
    .option("subscribe", "clicks")
    .load()
)

dims = spark.read.parquet("s3://warehouse/dim_product")

# stateful aggregation with NO withWatermark -> unbounded state growth
agg = (
    events
    .groupBy("user_id", "product_id")
    .agg(F.count("*").alias("clicks"))
)

# crossJoin explodes the stream against the dimension
enriched = agg.crossJoin(dims)

# pulling streaming results to the driver
sample = enriched.collect()
print("collected", len(sample))

# single output file forces all data through one task; no checkpointLocation
(
    enriched
    .coalesce(1)
    .writeStream
    .format("parquet")
    .option("path", "s3://warehouse/click_metrics")
    .start()
)
`,
  },
  {
    id: "flink-clicks",
    label: "Flink SQL — click windowing (intentionally bad)",
    format: "flink",
    language: "sql",
    code: `-- Source table with NO WATERMARK -> windows never fire on event time
CREATE TABLE clicks (
    user_id     BIGINT,
    product_id  BIGINT,
    event_ts    TIMESTAMP(3),
    referrer    STRING
) WITH (
    'connector' = 'kafka',
    'topic' = 'clicks',
    'properties.bootstrap.servers' = 'broker:9092',
    'format' = 'json',
    'scan.startup.mode' = 'latest-offset'
);

CREATE TABLE click_metrics (
    window_start TIMESTAMP(3),
    product_id   BIGINT,
    clicks       BIGINT
) WITH (
    'connector' = 'kafka',
    'topic' = 'click_metrics',
    'format' = 'json'
);

INSERT INTO click_metrics
SELECT
    TUMBLE_START(event_ts, INTERVAL '2' HOUR) AS window_start,
    product_id,
    COUNT(*) AS clicks
FROM clicks
GROUP BY
    TUMBLE(event_ts, INTERVAL '2' HOUR),   -- 2h tumbling window
    product_id
ORDER BY window_start;                      -- ORDER BY on an unbounded stream
`,
  },
  {
    id: "tpch-revenue",
    label: "TPC-H — top-revenue customers (intentionally bad)",
    format: "sql",
    language: "sql",
    code: `-- TPC-H benchmark: top-revenue customers report.
-- Tables: orders, lineitem, customer, supplier, nation, region.
CREATE OR REPLACE TABLE analytics.tpch_top_customers AS
SELECT
    c.*,                                    -- SELECT * ships every customer column
    n.n_name AS nation,
    r.r_name AS region,
    SUM(l.l_extendedprice * (1 - l.l_discount)) AS revenue,
    (
        SELECT COUNT(*)                     -- correlated scalar subquery in projection
        FROM tpch.orders o2
        WHERE o2.o_custkey = c.c_custkey
    ) AS lifetime_orders
FROM tpch.customer c,
     tpch.orders   o,                       -- comma cross join, no ON: cartesian product
     tpch.lineitem l,                        -- no date filter on lineitem (full scan)
     tpch.nation   n,
     tpch.region   r
WHERE o.o_custkey = c.c_custkey
  AND l.l_orderkey = o.o_orderkey
  AND c.c_nationkey = n.n_nationkey
  AND n.n_regionkey = r.r_regionkey
  AND c.c_mktsegment NOT IN (               -- NOT IN over a nullable subquery
        SELECT s.s_comment FROM tpch.supplier s
  )
  AND r.r_name LIKE '%AMERICA%'             -- leading-wildcard LIKE, full scan
GROUP BY c.c_custkey, c.c_name, c.c_acctbal, c.c_mktsegment, n.n_name, r.r_name
ORDER BY revenue DESC;
`,
  },
  {
    id: "nyc-taxi-metrics",
    label: "NYC TLC trip records — daily taxi metrics (intentionally bad)",
    format: "sql",
    language: "sql",
    code: `-- NYC TLC yellow_tripdata (open data): daily trip metrics.
-- Real columns: vendor_id, tpep_pickup_datetime, passenger_count,
-- trip_distance, fare_amount, tip_amount, total_amount, payment_type,
-- pulocationid, dolocationid.
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
GROUP BY vendor_id, pulocationid, dolocationid, payment_type
ORDER BY gross_revenue DESC;
`,
  },
  {
    id: "github-archive",
    label: "GH Archive — daily repo-activity rollup, BigQuery (intentionally bad)",
    format: "sql",
    language: "sql",
    code: `-- GH Archive (githubarchive public dataset, BigQuery): repo-activity rollup.
-- Real event schema: type, actor, repo, payload, created_at.
-- ANTI-PATTERN: scans EVERY daily shard of the events_* wildcard table because
-- it never filters on _TABLE_SUFFIX (nor created_at) -> terabytes per run.
CREATE OR REPLACE TABLE \`myproj.analytics.repo_activity_daily\` AS
SELECT
    *,                                              -- SELECT * on a huge wildcard table
    repo.name              AS repo_name,
    actor.login            AS actor_login,
    COUNT(*)               AS events,
    COUNTIF(type = 'PushEvent')         AS pushes,
    COUNTIF(type = 'PullRequestEvent')  AS pull_requests,
    COUNTIF(type = 'WatchEvent')        AS stars
FROM \`githubarchive.day.events_*\`
-- No _TABLE_SUFFIX filter and no created_at predicate -> every shard scanned.
WHERE type IN ('PushEvent', 'PullRequestEvent', 'WatchEvent', 'IssuesEvent')
  AND repo.name LIKE '%kubernetes%'                 -- leading-wildcard LIKE, full scan
GROUP BY repo.name, actor.login, type
ORDER BY events DESC;
`,
  },
  {
    id: "imdb-top-titles",
    label: "IMDb datasets — top-rated titles join (intentionally bad)",
    format: "sql",
    language: "sql",
    code: `-- IMDb non-commercial datasets (open): top-rated titles join.
-- title_basics(tconst, primarytitle, startyear, genres)
-- title_ratings(tconst, averagerating, numvotes)
CREATE OR REPLACE TABLE analytics.imdb_top_titles AS
SELECT
    b.*,                                        -- SELECT * ships every title column
    r.averagerating,
    r.numvotes,
    (
        SELECT COUNT(*)                         -- correlated scalar subquery in projection
        FROM imdb.title_basics b2
        WHERE b2.startyear = b.startyear
    ) AS titles_in_year
FROM imdb.title_basics  b
JOIN imdb.title_ratings r
  ON r.tconst = b.tconst
WHERE r.numvotes > 10000
  AND r.averagerating >= 8.0
  AND b.primarytitle LIKE '%Star%'              -- leading-wildcard LIKE on primarytitle
  AND b.tconst NOT IN (                         -- NOT IN over a nullable subquery
        SELECT a.tconst FROM imdb.title_akas a
  )
ORDER BY r.averagerating DESC, r.numvotes DESC;
`,
  },
];

export const DEFAULT_SAMPLE: Sample = samples[0];
