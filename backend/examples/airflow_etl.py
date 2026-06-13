"""Daily customer-orders ETL DAG.

Deliberately exhibits operational anti-patterns: zero retries, catchup never
set, a poke-mode sensor, an XCom that ships a whole DataFrame, three heavy
SparkSubmit tasks chained strictly sequentially, and no SLA / owner /
on_failure_callback.
"""
from datetime import datetime

import pandas as pd
from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.providers.amazon.aws.sensors.s3 import S3KeySensor
from airflow.providers.apache.spark.operators.spark_submit import SparkSubmitOperator

default_args = {"retries": 0}


def extract_orders(**context):
    """Pull the raw orders extract and push it through XCom (anti-pattern)."""
    df = pd.read_parquet("s3://raw/orders/latest.parquet")
    # Pushing an entire DataFrame through XCom serializes it into the metadata DB.
    context["ti"].xcom_push(key="orders", value=df.to_dict())


def load_summary(**context):
    """Pull the DataFrame back out of XCom and write a summary."""
    raw = context["ti"].xcom_pull(key="orders")
    pd.DataFrame.from_dict(raw).to_parquet("s3://curated/orders_summary.parquet")


with DAG(
    dag_id="customer_orders_etl",
    start_date=datetime(2023, 1, 1),
    schedule_interval="@daily",
    default_args=default_args,
) as dag:
    wait_for_drop = S3KeySensor(
        task_id="wait_for_orders_drop",
        bucket_key="s3://raw/orders/_SUCCESS",
        mode="poke",  # poke mode holds a worker slot the whole time
        poke_interval=60,
        timeout=60 * 60 * 6,
    )

    extract = PythonOperator(task_id="extract_orders", python_callable=extract_orders)

    enrich = SparkSubmitOperator(task_id="spark_enrich", application="jobs/enrich.py")
    aggregate = SparkSubmitOperator(task_id="spark_aggregate", application="jobs/aggregate.py")
    rollup = SparkSubmitOperator(task_id="spark_rollup", application="jobs/rollup.py")

    load = PythonOperator(task_id="load_summary", python_callable=load_summary)

    # Three heavy Spark stages chained strictly one-after-another.
    wait_for_drop >> extract >> enrich >> aggregate >> rollup >> load
