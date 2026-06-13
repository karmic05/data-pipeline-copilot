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
];

export const DEFAULT_SAMPLE: Sample = samples[0];
