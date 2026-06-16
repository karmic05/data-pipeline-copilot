"""Streaming pipeline rules.

Deterministic checks for streaming jobs (Spark Structured Streaming, Flink,
Kafka Streams). A job is considered streaming when its parser sets
``ir.materialization.type == "stream"``. Operation ``details`` follow the
shared streaming conventions (WINDOW ``{kind, size_minutes}``, STATE
``{has_ttl}``, SOURCE/SINK ``{connector | topic}``, REPARTITION
``{target_partitions}``, ORDER_BY/DISTINCT/COLLECT ``{on_stream}``) and are
always read defensively with ``.get`` - each parser populates them best-effort.
"""
from __future__ import annotations

import logging
import re

from app.rules import Rule, register
from app.schemas.ir import Operation, ParseResult
from app.schemas.report import Issue

logger = logging.getLogger(__name__)

STREAMING_FORMATS = frozenset({"spark", "flink", "kafka"})

_STREAM_WINDOW_KINDS = frozenset(
    {"tumbling", "tumble", "hopping", "hop", "sliding", "slide", "session", "cumulate"}
)
_SINGLE_PARTITION_RE = re.compile(r"\.(?:repartition|coalesce)\s*\(\s*1\s*\)")
_GROUP_ID_RE = re.compile(
    r"group[._\s-]?id|consumer[._\s-]?group|application[._\s-]?id", re.IGNORECASE
)


def _is_streaming(pr: ParseResult) -> bool:
    """True when the parsed pipeline is a streaming job."""
    return pr.ir.materialization.type == "stream"


def _op_line(op: Operation) -> int | None:
    """1-based line of an operation, or None when the parser had no location."""
    line = op.location.line if op.location else 0
    return line if line and line > 0 else None


def _as_number(value: object) -> float | None:
    """Coerce a details value to float, returning None for malformed input."""
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _window_kind(op: Operation) -> str:
    """Lower-cased window kind from op details (empty string when absent)."""
    return str(op.details.get("kind", "") or "").lower()


# ---------------------------------------------------------------------------
# CRITICAL
# ---------------------------------------------------------------------------


@register
class UnboundedStateRule(Rule):
    """Stateful streaming operators without a TTL grow until the job dies."""

    id = "UNBOUNDED_STATE"
    severity = "CRITICAL"
    category = "reliability"
    formats = STREAMING_FORMATS
    title = "Unbounded state store"
    description = (
        "A stateful streaming operator (aggregation, join, deduplication) has no "
        "TTL or retention configured, so its state grows without bound until the "
        "job exhausts memory or disk."
    )

    _FIXES = {
        "spark": (
            "Add a watermark plus state timeout, e.g. .withWatermark('event_time', "
            "'1 hour') before the stateful operator, or use mapGroupsWithState with "
            "a GroupStateTimeout so stale keys are evicted."
        ),
        "flink": (
            "Configure state TTL, e.g. SET 'table.exec.state.ttl' = '24 h' (Flink "
            "SQL) or StateTtlConfig.newBuilder(Time.hours(24)) in the DataStream API."
        ),
        "kafka": (
            "Use windowed stores with retention, e.g. Materialized.as(...)"
            ".withRetention(Duration.ofHours(24)), instead of unbounded KTable state."
        ),
    }

    def check(self, pr: ParseResult) -> list[Issue]:
        """Flag every STATE op whose details report no TTL."""
        issues: list[Issue] = []
        for op in pr.ir.ops("STATE"):
            if op.details.get("has_ttl"):
                continue
            store = op.details.get("store") or op.details.get("name") or "stateful operator"
            issues.append(
                self.issue(
                    f"Streaming state in '{store}' has no TTL/retention configured; "
                    "every new key adds state forever, eventually exhausting memory "
                    "or RocksDB disk and crashing the job.",
                    line=_op_line(op),
                    fix_suggestion=self._FIXES.get(
                        pr.ir.format,
                        "Configure a state TTL or retention window so old keys are evicted.",
                    ),
                )
            )
        return issues


@register
class MissingWatermarkRule(Rule):
    """Event-time windows without a watermark can never finalize state."""

    id = "MISSING_WATERMARK"
    severity = "CRITICAL"
    category = "reliability"
    formats = STREAMING_FORMATS
    title = "Missing watermark"
    description = (
        "Event-time windowed aggregation is used but no watermark is declared, so "
        "the engine cannot decide when a window is complete: late data is mishandled "
        "and window state is held indefinitely."
    )

    _FIXES = {
        "spark": (
            'Declare a watermark before the window, e.g. .withWatermark("event_time", '
            '"10 minutes"), so windows can close and state can be dropped.'
        ),
        "flink": (
            "Add WATERMARK FOR event_time AS event_time - INTERVAL '10' SECOND to the "
            "source table DDL (or assign a WatermarkStrategy in the DataStream API)."
        ),
        "kafka": (
            "Bound lateness with a grace period, e.g. TimeWindows.ofSizeAndGrace("
            "Duration.ofMinutes(5), Duration.ofMinutes(1)), and route late records "
            "explicitly."
        ),
    }

    def check(self, pr: ParseResult) -> list[Issue]:
        """Fire once when a streaming job has event-time windows but no WATERMARK op."""
        if not _is_streaming(pr):
            return []
        if pr.ir.ops("WATERMARK"):
            return []
        event_windows = [
            op
            for op in pr.ir.ops("WINDOW")
            if not _window_kind(op) or _window_kind(op) in _STREAM_WINDOW_KINDS
        ]
        if not event_windows:
            return []
        first = event_windows[0]
        kind = _window_kind(first) or "event-time"
        return [
            self.issue(
                f"A {kind} window is defined but the job declares no watermark; "
                "out-of-order events will be silently dropped or window state will "
                "be retained forever.",
                line=_op_line(first),
                fix_suggestion=self._FIXES.get(
                    pr.ir.format,
                    "Declare an event-time watermark so windows can close deterministically.",
                ),
            )
        ]


@register
class NoCheckpointingRule(Rule):
    """A streaming job without checkpointing loses progress on every failure."""

    id = "NO_CHECKPOINTING"
    severity = "CRITICAL"
    category = "reliability"
    formats = STREAMING_FORMATS
    title = "No checkpointing"
    description = (
        "The streaming job has no checkpoint configuration, so on any restart it "
        "loses offsets and operator state - causing data loss or full reprocessing."
    )

    _FIXES = {
        "spark": (
            'Set a durable checkpoint location on the writer: .option("checkpointLocation", '
            '"s3://your-bucket/checkpoints/this_job") before .start().'
        ),
        "flink": (
            "Enable checkpointing: SET 'execution.checkpointing.interval' = '60 s' "
            "(SQL) or env.enableCheckpointing(60000) in the DataStream API."
        ),
        "kafka": (
            "Set processing.guarantee=exactly_once_v2 and a durable state.dir so the "
            "topology recovers offsets and state after a restart."
        ),
    }

    _DIFFS = {
        "spark": (
            "--- current\n"
            "+++ optimized\n"
            " query = (df.writeStream\n"
            '+    .option("checkpointLocation", "s3://your-bucket/checkpoints/this_job")\n'
            "     .start())"
        ),
        "flink": (
            "--- current\n"
            "+++ optimized\n"
            "+SET 'execution.checkpointing.interval' = '60 s';\n"
            "+SET 'execution.checkpointing.mode' = 'EXACTLY_ONCE';\n"
            " -- existing job statements"
        ),
    }

    def check(self, pr: ParseResult) -> list[Issue]:
        """Fire once for streaming jobs that emit no CHECKPOINT operation."""
        if not _is_streaming(pr) or pr.ir.ops("CHECKPOINT"):
            return []
        return [
            self.issue(
                "Streaming job has no checkpointing configured; a restart or crash "
                "will lose consumed offsets and in-flight state, causing data loss "
                "or duplicate reprocessing.",
                fix_suggestion=self._FIXES.get(
                    pr.ir.format, "Enable durable checkpointing for the streaming job."
                ),
                fix_diff=self._DIFFS.get(pr.ir.format),
            )
        ]


# ---------------------------------------------------------------------------
# WARNING
# ---------------------------------------------------------------------------


@register
class WideTumblingWindowRule(Rule):
    """Very wide windows hold large state and delay results until close."""

    id = "WIDE_TUMBLING_WINDOW"
    severity = "WARNING"
    category = "performance"
    formats = STREAMING_FORMATS
    title = "Wide tumbling window"
    description = (
        "A window wider than 60 minutes buffers a large amount of state per key and "
        "emits results only when the window closes, hurting both memory and latency."
    )

    def check(self, pr: ParseResult) -> list[Issue]:
        """Flag WINDOW ops whose size_minutes exceeds 60."""
        issues: list[Issue] = []
        for op in pr.ir.ops("WINDOW"):
            size = _as_number(op.details.get("size_minutes"))
            if size is None or size <= 60:
                continue
            kind = _window_kind(op)
            if kind and kind not in _STREAM_WINDOW_KINDS and not _is_streaming(pr):
                continue
            issues.append(
                self.issue(
                    f"{(kind or 'tumbling').capitalize()} window of {size:g} minutes "
                    "holds state for the full window and delays output until it "
                    "closes; downstream consumers see results up to "
                    f"{size:g} minutes late.",
                    line=_op_line(op),
                    fix_suggestion=(
                        "Shrink the window, or aggregate incrementally: compute "
                        "small (e.g. 5-minute) windows in the stream and roll them "
                        "up to the wide interval in a downstream batch/OLAP layer."
                    ),
                )
            )
        return issues


@register
class BlockingOperationRule(Rule):
    """Global sorts / distincts cannot complete on an unbounded stream."""

    id = "BLOCKING_OPERATION"
    severity = "WARNING"
    category = "performance"
    formats = STREAMING_FORMATS
    title = "Blocking operation on stream"
    description = (
        "ORDER BY / DISTINCT over an unbounded stream requires unbounded buffering "
        "(or is rejected outright by the engine) because the full input never arrives."
    )

    def check(self, pr: ParseResult) -> list[Issue]:
        """Flag ORDER_BY / DISTINCT ops marked on_stream by the parser."""
        issues: list[Issue] = []
        for op in pr.ir.ops("ORDER_BY", "DISTINCT"):
            if not op.details.get("on_stream"):
                continue
            label = "ORDER BY" if op.type == "ORDER_BY" else "DISTINCT"
            issues.append(
                self.issue(
                    f"{label} is applied to an unbounded stream; it must buffer "
                    "all input before emitting, which never completes and grows "
                    "state without limit.",
                    line=_op_line(op),
                    fix_suggestion=(
                        "Scope the operation to a window (windowed dedup / per-window "
                        "ordering with event-time + watermark) or move global "
                        "sorting/dedup to a downstream batch step."
                    ),
                )
            )
        return issues


@register
class NoBackpressureHandlingRule(Rule):
    """Unthrottled streaming sources fall over under input spikes."""

    id = "NO_BACKPRESSURE_HANDLING"
    severity = "WARNING"
    category = "reliability"
    formats = STREAMING_FORMATS
    title = "No backpressure handling"
    description = (
        "The streaming source has no rate limit or backpressure configuration; a "
        "burst of input can overwhelm the job, causing OOMs, growing lag, or "
        "checkpoint timeouts."
    )

    _FIXES = {
        "spark": (
            'Bound each micro-batch, e.g. .option("maxOffsetsPerTrigger", 100000) '
            'for Kafka sources or .option("maxFilesPerTrigger", 10) for file sources.'
        ),
        "flink": (
            "Tune source parallelism and enable buffer debloating "
            "('taskmanager.network.memory.buffer-debloat.enabled' = 'true'); watch "
            "the backpressure panel in the Flink UI."
        ),
        "kafka": (
            "Bound consumption with max.poll.records / fetch.max.bytes and size "
            "buffered.records.per.partition so the topology degrades gracefully."
        ),
    }

    _RATE_LIMIT_KEYS = (
        "max_offsets_per_trigger",
        "max_files_per_trigger",
        "max_rate",
        "rate_limit",
        "max_poll_records",
    )

    def check(self, pr: ParseResult) -> list[Issue]:
        """Fire once for streaming jobs with sources but no backpressure config."""
        if not _is_streaming(pr):
            return []
        sources = pr.ir.ops("SOURCE")
        if not sources:
            return []
        if pr.extras.get("has_backpressure_config"):
            return []
        for op in sources:
            if any(op.details.get(key) for key in self._RATE_LIMIT_KEYS):
                return []
        first = sources[0]
        origin = first.details.get("connector") or first.details.get("topic")
        origin_label = f" '{origin}'" if origin else ""
        return [
            self.issue(
                f"Streaming source{origin_label} has no backpressure or rate-limit "
                "configuration; an input spike will overload the job instead of "
                "being absorbed gradually.",
                line=_op_line(first),
                fix_suggestion=self._FIXES.get(
                    pr.ir.format,
                    "Configure source-side rate limiting so input bursts cannot "
                    "overwhelm the job.",
                ),
            )
        ]


@register
class RepartitionShuffleRule(Rule):
    """Mid-stream repartitioning forces a full network shuffle per batch."""

    id = "REPARTITION_SHUFFLE"
    severity = "WARNING"
    category = "performance"
    formats = STREAMING_FORMATS
    title = "Repartition shuffle mid-stream"
    description = (
        "Repartitioning inside a streaming pipeline forces a full shuffle of every "
        "micro-batch/record exchange, adding latency and network cost on every trigger."
    )

    def check(self, pr: ParseResult) -> list[Issue]:
        """Flag REPARTITION ops in streaming jobs (target 1 is handled separately)."""
        if not _is_streaming(pr):
            return []
        issues: list[Issue] = []
        for op in pr.ir.ops("REPARTITION"):
            target = _as_number(op.details.get("target_partitions"))
            if target is not None and int(target) == 1:
                continue  # SINGLE_PARTITION_SINK owns this case
            target_label = f" to {int(target)} partitions" if target is not None else ""
            issues.append(
                self.issue(
                    f"repartition{target_label} inside the stream shuffles every "
                    "batch across the network before processing continues, adding "
                    "latency on every trigger.",
                    line=_op_line(op),
                    fix_suggestion=(
                        "Partition correctly at the source (topic partitioning / "
                        "source parallelism) instead of reshuffling mid-stream, or "
                        "remove the repartition if the downstream operator does not "
                        "need it."
                    ),
                )
            )
        return issues


@register
class CollectToDriverRule(Rule):
    """collect() funnels the distributed dataset into one process."""

    id = "COLLECT_TO_DRIVER"
    severity = "WARNING"
    category = "performance"
    formats = STREAMING_FORMATS
    title = "Collect to driver"
    description = (
        "collect()/toPandas()-style operations pull the entire distributed dataset "
        "into the driver process, which does not scale and can OOM the driver - in "
        "batch jobs and, worse, on every streaming trigger."
    )

    def check(self, pr: ParseResult) -> list[Issue]:
        """Flag every COLLECT op (batch and streaming)."""
        issues: list[Issue] = []
        for op in pr.ir.ops("COLLECT"):
            on_stream = bool(op.details.get("on_stream"))
            suffix = (
                " Inside a streaming job this repeats on every trigger, so driver "
                "memory pressure compounds continuously."
                if on_stream
                else ""
            )
            issues.append(
                self.issue(
                    "collect() materializes the full distributed dataset on the "
                    "driver, creating a single-process bottleneck and OOM risk."
                    + suffix,
                    line=_op_line(op),
                    fix_suggestion=(
                        "Write results to a sink (write/writeStream, foreachBatch) "
                        "or aggregate first so only small summaries ever reach the "
                        "driver; use take(n)/limit for sampling."
                    ),
                )
            )
        return issues


@register
class SinglePartitionSinkRule(Rule):
    """repartition(1)/coalesce(1) serializes the entire write."""

    id = "SINGLE_PARTITION_SINK"
    severity = "WARNING"
    category = "performance"
    formats = STREAMING_FORMATS
    title = "Single-partition sink"
    description = (
        "An explicit repartition(1)/coalesce(1) before the write funnels all data "
        "through one partition, turning a parallel write into a single giant task."
    )

    _FIX = (
        "Remove the explicit single-partition step and let the engine write in "
        "parallel; if consumers need one file, compact afterwards (e.g. OPTIMIZE / "
        "a small downstream merge job) instead of serializing the hot path."
    )

    def check(self, pr: ParseResult) -> list[Issue]:
        """Flag repartition(1)/coalesce(1) via REPARTITION ops, then source scan."""
        issues: list[Issue] = []
        flagged_lines: set[int] = set()
        for op in pr.ir.ops("REPARTITION"):
            target = _as_number(op.details.get("target_partitions"))
            if target is None or int(target) != 1:
                continue
            line = _op_line(op)
            if line is not None:
                flagged_lines.add(line)
            issues.append(
                self.issue(
                    "Data is repartitioned to a single partition before the sink; "
                    "the entire output is written by one task, erasing all write "
                    "parallelism.",
                    line=line,
                    fix_suggestion=self._FIX,
                )
            )
        for line_no, text in enumerate(pr.lines, start=1):
            if line_no in flagged_lines or not _SINGLE_PARTITION_RE.search(text):
                continue
            flagged_lines.add(line_no)
            issues.append(
                self.issue(
                    "repartition(1)/coalesce(1) forces all output through a single "
                    "partition, serializing the write into one giant task.",
                    line=line_no,
                    fix_suggestion=self._FIX,
                )
            )
        return issues


# ---------------------------------------------------------------------------
# INFO
# ---------------------------------------------------------------------------


@register
class NoConsumerGroupNamingRule(Rule):
    """Unnamed consumer groups make lag monitoring and ops handoffs hard."""

    id = "NO_CONSUMER_GROUP_NAMING"
    severity = "INFO"
    category = "observability"
    formats = STREAMING_FORMATS
    title = "No explicit consumer group name"
    description = (
        "Kafka sources without an explicit, stable consumer group (group.id / "
        "application.id) get auto-generated groups: consumer lag cannot be tracked "
        "across restarts and offset history is lost on every redeploy."
    )

    _GROUP_DETAIL_KEYS = ("group_id", "group.id", "group", "consumer_group", "application_id")

    def check(self, pr: ParseResult) -> list[Issue]:
        """Fire once for streaming Kafka sources with no group id evidence."""
        if not _is_streaming(pr):
            return []
        sources = pr.ir.ops("SOURCE")
        kafka_sources = [
            op
            for op in sources
            if "kafka" in str(op.details.get("connector", "") or "").lower()
            or op.details.get("topic")
        ]
        if not kafka_sources and pr.ir.format == "kafka":
            kafka_sources = sources
        if not kafka_sources:
            return []
        for op in kafka_sources:
            if any(op.details.get(key) for key in self._GROUP_DETAIL_KEYS):
                return []
        if pr.extras.get("consumer_group") or pr.extras.get("group_id"):
            return []
        if _GROUP_ID_RE.search(pr.source):
            return []
        first = kafka_sources[0]
        topic = first.details.get("topic") or first.details.get("connector")
        topic_label = f" reading '{topic}'" if topic else ""
        return [
            self.issue(
                f"Kafka source{topic_label} sets no explicit consumer group; an "
                "auto-generated group id changes on restart, so committed offsets "
                "and lag metrics do not survive redeploys.",
                line=_op_line(first),
                fix_suggestion=(
                    "Set a stable, descriptive group id (e.g. group.id="
                    "'orders-enrichment-v1' or Kafka Streams application.id) and "
                    "monitor its lag with standard consumer-group tooling."
                ),
            )
        ]
