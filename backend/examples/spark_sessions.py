"""PySpark structured streaming sessionization job.

Deliberately exhibits streaming anti-patterns: a stateful groupBy aggregation
with no withWatermark, no checkpointLocation on the sink, a crossJoin, a
.collect() pulling data to the driver, and a .coalesce(1).write bottleneck.
"""
from pyspark.sql import SparkSession
from pyspark.sql import functions as F

spark = SparkSession.builder.appName("sessionizer").getOrCreate()

clicks = (
    spark.readStream
    .format("kafka")
    .option("kafka.bootstrap.servers", "broker:9092")
    .option("subscribe", "clicks")
    .load()
)

dim = spark.read.parquet("s3://warehouse/dim_pages")

# crossJoin against a dimension blows the stream up combinatorially.
enriched = clicks.crossJoin(dim)

# Stateful aggregation with NO withWatermark: state grows without bound.
sessions = (
    enriched
    .groupBy("user_id", F.window("event_time", "10 minutes"))
    .agg(F.count("*").alias("clicks"))
)

# Pulling streaming results to the driver defeats the whole point.
sample = sessions.collect()
print(sample[:5])

# coalesce(1) funnels every partition through a single task, and the write
# has no checkpointLocation so the query cannot recover after a restart.
(
    sessions.coalesce(1)
    .writeStream
    .format("parquet")
    .option("path", "s3://curated/sessions")
    .outputMode("complete")
    .start()
)
