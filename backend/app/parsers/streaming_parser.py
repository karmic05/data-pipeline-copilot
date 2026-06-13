"""Static parser for Kafka streaming topologies.

``parse_kafka`` handles two families of input:

- Python Kafka clients (``kafka-python``, ``confluent_kafka``, ``faust``),
  analyzed with the stdlib :mod:`ast` module — never imported or executed.
- Java/Scala Kafka Streams DSL (``StreamsBuilder``/``KStream``), which cannot
  be AST-parsed as Python; per the module contract this one format is scanned
  line-by-line with regular expressions instead.

Both paths emit ``SOURCE``/``SINK``/``WINDOW``/``STATE`` operations, topic
:class:`TableRef` entries, ``materialization.type="stream"`` and
``extras["has_backpressure_config"]``.
"""
from __future__ import annotations

import ast
import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from app.schemas.ir import (
    IR,
    Dependency,
    IRMetadata,
    Location,
    Materialization,
    Operation,
    ParseError,
    ParseResult,
    TableRef,
)

logger = logging.getLogger(__name__)

#: Config keys (Python or Java form) that bound consumption / buffering.
_BACKPRESSURE_TOKENS = frozenset(
    {
        "max.poll.records",
        "max_poll_records",
        "queued.max.messages",
        "cache.max.bytes.buffering",
        "buffered.records.per.partition",
    }
)
_BACKPRESSURE_KWARGS = frozenset({"max_poll_records", "buffer_maxsize"})

# --- Java/Scala Kafka Streams DSL line-scanning patterns (contract-sanctioned
# --- regex use: Java cannot be parsed with the Python ast module). -----------
_JAVA_MARKERS = re.compile(
    r"new\s+StreamsBuilder|new\s+KafkaStreams|KStream<|KTable<|Serdes\.|"
    r"StreamsConfig\.|public\s+(?:static\s+)?[\w<>\[\]]+\s+\w+\s*\("
)
_STREAMS_DSL_TOKENS = re.compile(
    r"builder\.(?:stream|table|globalTable)|windowedBy|KafkaStreams|groupByKey"
)
_J_SOURCE = re.compile(r"\.(?:stream|table|globalTable)\s*\(\s*\"([^\"]+)\"")
_J_SINK = re.compile(r"\.to\s*\(\s*\"([^\"]+)\"")
_J_APP_ID = re.compile(r"(?:APPLICATION_ID_CONFIG|\"application\.id\")\s*,\s*\"([^\"]+)\"")
_J_WINDOW = re.compile(r"windowedBy|TimeWindows|SessionWindows|SlidingWindows")
_J_DURATION = re.compile(r"Duration\.of(Millis|Seconds|Minutes|Hours|Days)\s*\(\s*(\d+)")
_J_STATE = re.compile(r"\.(?:aggregate|count|reduce)\s*\(|Materialized\.|Stores\.")
_J_GROUP = re.compile(r"\.groupByKey\s*\(|\.groupBy\s*\(")
_J_RETENTION = re.compile(r"withRetention|retention\.ms")
_J_BACKPRESSURE = re.compile(
    r"max\.poll\.records|buffered\.records\.per\.partition|cache\.max\.bytes\.buffering"
)
_DURATION_TO_MINUTES = {
    "Millis": 1 / 60000,
    "Seconds": 1 / 60,
    "Minutes": 1.0,
    "Hours": 60.0,
    "Days": 1440.0,
}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _literal(node: Optional[ast.AST]) -> Any:
    """Best-effort literal evaluation; ``None`` for non-literal nodes."""
    if node is None:
        return None
    try:
        return ast.literal_eval(node)
    except Exception:  # noqa: BLE001 - any non-literal node lands here
        return None


def _str_literal(node: Optional[ast.AST]) -> Optional[str]:
    """A string literal value, or ``None``."""
    value = _literal(node)
    return value if isinstance(value, str) else None


def _terminal_name(node: Optional[ast.AST]) -> str:
    """The trailing identifier of a Name/Attribute chain."""
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _root_name(node: Optional[ast.AST]) -> str:
    """The leading identifier of a Name/Attribute/Call chain."""
    cur = node
    while True:
        if isinstance(cur, ast.Call):
            cur = cur.func
        elif isinstance(cur, ast.Attribute):
            cur = cur.value
        elif isinstance(cur, ast.Name):
            return cur.id
        else:
            return ""


def _chain_calls(node: ast.AST) -> List[Tuple[str, ast.Call]]:
    """``[(method_name, call_node), ...]`` for every call in a method chain."""
    out: List[Tuple[str, ast.Call]] = []
    cur: Optional[ast.AST] = node
    while cur is not None:
        if isinstance(cur, ast.Call):
            out.append((_terminal_name(cur.func), cur))
            cur = cur.func
        elif isinstance(cur, ast.Attribute):
            cur = cur.value
        else:
            break
    out.reverse()
    return out


def _kwargs_map(call: Optional[ast.Call]) -> Dict[str, ast.AST]:
    """Keyword arguments of a call as ``{name: value_node}``."""
    if not isinstance(call, ast.Call):
        return {}
    return {kw.arg: kw.value for kw in call.keywords if kw.arg}


def _timedelta_minutes(node: Optional[ast.AST]) -> Optional[float]:
    """Total minutes represented by a ``timedelta(...)`` call node, if any."""
    if not isinstance(node, ast.Call) or _terminal_name(node.func) != "timedelta":
        return None
    positional = ("days", "seconds", "microseconds", "milliseconds", "minutes", "hours", "weeks")
    values: Dict[str, float] = {}
    for i, arg in enumerate(node.args[: len(positional)]):
        val = _literal(arg)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            values[positional[i]] = float(val)
    for kw in node.keywords:
        if kw.arg:
            val = _literal(kw.value)
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                values[kw.arg] = float(val)
    return (
        values.get("weeks", 0.0) * 7 * 1440
        + values.get("days", 0.0) * 1440
        + values.get("hours", 0.0) * 60
        + values.get("minutes", 0.0)
        + values.get("seconds", 0.0) / 60
        + values.get("milliseconds", 0.0) / 60000
        + values.get("microseconds", 0.0) / 60_000_000
    )


def _window_size_minutes(node: Optional[ast.AST]) -> Optional[float]:
    """Window size in minutes from a timedelta call or a numeric seconds value."""
    minutes = _timedelta_minutes(node)
    if minutes is not None:
        return minutes
    value = _literal(node)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) / 60
    return None


def _location(node: ast.AST) -> Location:
    """1-based source location for an AST node."""
    return Location(
        line=int(getattr(node, "lineno", 0) or 0),
        col=int(getattr(node, "col_offset", 0) or 0),
    )


def _build_result(
    *,
    source: str,
    ast_obj: Any,
    ops: List[Operation],
    source_topics: List[str],
    sink_topics: List[str],
    app_id: Optional[str],
    has_backpressure: bool,
    warnings: List[str],
) -> ParseResult:
    """Assemble the streaming ParseResult shared by the Python and Java paths."""
    access: Dict[str, str] = {}
    for topic in source_topics:
        access[topic] = "readwrite" if access.get(topic) == "write" else "read"
    for topic in sink_topics:
        access[topic] = (
            "readwrite" if access.get(topic) == "read" else access.get(topic, "write")
        )
    tables = [TableRef(name=name, access_type=acc) for name, acc in access.items()]

    dependencies: List[Dependency] = []
    seen: Set[Tuple[str, str]] = set()
    for src in dict.fromkeys(source_topics):
        for dst in dict.fromkeys(sink_topics):
            if src != dst and (src, dst) not in seen:
                seen.add((src, dst))
                dependencies.append(Dependency(source=src, target=dst, type="writes_to"))

    ops.sort(key=lambda op: (op.location.line, op.location.col))
    ir = IR(
        format="kafka",
        tables=tables,
        operations=ops,
        dependencies=dependencies,
        materialization=Materialization(type="stream"),
        metadata=IRMetadata(name=app_id),
    )
    logger.debug(
        "parse_kafka: %d op(s), %d topic(s), backpressure=%s",
        len(ops),
        len(tables),
        has_backpressure,
    )
    return ParseResult(
        ir=ir,
        source=source,
        ast=ast_obj,
        extras={"has_backpressure_config": has_backpressure},
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_kafka(source: str) -> ParseResult:
    """Parse a Kafka topology (Python clients or Java Streams DSL) into the IR.

    Python sources are statically analyzed with :mod:`ast`. Java/Scala Kafka
    Streams sources are scanned line-by-line (the contract-sanctioned
    exception, since Java is not Python-parseable).

    Raises:
        ParseError: when Python source has a syntax error (with line number)
            and the content does not look like the Kafka Streams DSL.
    """
    if _JAVA_MARKERS.search(source):
        return _parse_java_streams(
            source,
            ["Java/Scala Kafka Streams source detected; analyzed via line scanning."],
        )
    try:
        module = ast.parse(source)
    except SyntaxError as exc:
        if _STREAMS_DSL_TOKENS.search(source):
            return _parse_java_streams(
                source,
                [
                    "Source is not valid Python; Kafka Streams DSL analyzed "
                    "via line scanning."
                ],
            )
        raise ParseError(
            f"Python syntax error in Kafka client source: {exc.msg}",
            line=exc.lineno or 1,
        ) from exc
    return _parse_python_kafka(source, module)


# ---------------------------------------------------------------------------
# Python clients (kafka-python / confluent_kafka / faust)
# ---------------------------------------------------------------------------

def _parse_python_kafka(source: str, module: ast.Module) -> ParseResult:
    """AST analysis of Python Kafka client / faust agent code."""
    try:
        warnings: List[str] = []
        ops: List[Operation] = []
        source_topics: List[str] = []
        sink_topics: List[str] = []
        app_id: Optional[str] = None
        has_backpressure = False

        # Pass 1 — topic handles: ``orders = app.topic("orders")``.
        topic_vars: Dict[str, str] = {}
        for node in ast.walk(module):
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
                continue
            call = node.value
            if (
                isinstance(call.func, ast.Attribute)
                and call.func.attr == "topic"
                and call.args
            ):
                topic = _str_literal(call.args[0])
                if topic:
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            topic_vars[target.id] = topic

        # STATE candidates keyed by the Table call node, so a chained
        # ``.tumbling(..., expires=...)`` can mark TTL on its own table.
        state_records: Dict[int, Dict[str, Any]] = {}

        for node in ast.walk(module):
            if isinstance(node, ast.Constant) and node.value in _BACKPRESSURE_TOKENS:
                has_backpressure = True

            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for dec in node.decorator_list:
                    if not isinstance(dec, ast.Call):
                        continue
                    if _terminal_name(dec.func) != "agent":
                        continue
                    topic = None
                    if dec.args:
                        arg = dec.args[0]
                        if isinstance(arg, ast.Name):
                            topic = topic_vars.get(arg.id)
                        elif isinstance(arg, ast.Call) and arg.args:
                            topic = _str_literal(arg.args[0])
                    ops.append(
                        Operation(
                            type="SOURCE",
                            location=_location(node),
                            details={"topic": topic},
                        )
                    )
                    if topic:
                        source_topics.append(topic)
                continue

            if not isinstance(node, ast.Call):
                continue
            kwargs = _kwargs_map(node)
            if any(k in _BACKPRESSURE_KWARGS for k in kwargs):
                has_backpressure = True

            tname = _terminal_name(node.func)
            if tname == "KafkaConsumer":
                for arg in node.args:
                    topic = _str_literal(arg)
                    if topic:
                        ops.append(
                            Operation(
                                type="SOURCE",
                                location=_location(node),
                                details={"topic": topic},
                            )
                        )
                        source_topics.append(topic)
                continue
            if tname == "App" and node.args and app_id is None:
                app_id = _str_literal(node.args[0])
                continue
            if not isinstance(node.func, ast.Attribute):
                continue

            attr = node.func.attr
            if attr == "subscribe":
                topics_node = node.args[0] if node.args else kwargs.get("topics")
                topics: List[str] = []
                single = _str_literal(topics_node)
                if single:
                    topics = [single]
                else:
                    value = _literal(topics_node)
                    if isinstance(value, (list, tuple)):
                        topics = [t for t in value if isinstance(t, str)]
                for topic in topics:
                    ops.append(
                        Operation(
                            type="SOURCE",
                            location=_location(node),
                            details={"topic": topic},
                        )
                    )
                    source_topics.append(topic)
            elif attr in {"send", "produce"}:
                topic = (
                    (_str_literal(node.args[0]) if node.args else None)
                    or _str_literal(kwargs.get("topic"))
                    or topic_vars.get(_root_name(node.func.value))
                )
                ops.append(
                    Operation(
                        type="SINK",
                        location=_location(node),
                        details={"topic": topic},
                    )
                )
                if topic:
                    sink_topics.append(topic)
            elif attr in {"tumbling", "hopping"}:
                size_node = node.args[0] if node.args else kwargs.get("size")
                ops.append(
                    Operation(
                        type="WINDOW",
                        location=_location(node),
                        details={
                            "kind": attr,
                            "size_minutes": _window_size_minutes(size_node),
                        },
                    )
                )
                if "expires" in kwargs:
                    for name, call in _chain_calls(node):
                        if name in {"Table", "GlobalTable"}:
                            entry = state_records.get(id(call))
                            if entry is not None:
                                entry["has_ttl"] = True
            elif attr in {"Table", "GlobalTable"}:
                state_records[id(node)] = {
                    "node": node,
                    "has_ttl": "expires" in kwargs,
                }
            elif attr == "group_by":
                ops.append(
                    Operation(
                        type="STATE",
                        location=_location(node),
                        details={"has_ttl": False},
                    )
                )

        # Second chance for TTL marks: window chains are walked after their
        # Table call in some traversal orders, so re-scan chains once more.
        for node in ast.walk(module):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr in {"tumbling", "hopping"} and "expires" in _kwargs_map(node):
                for name, call in _chain_calls(node):
                    if name in {"Table", "GlobalTable"} and id(call) in state_records:
                        state_records[id(call)]["has_ttl"] = True

        for entry in state_records.values():
            ops.append(
                Operation(
                    type="STATE",
                    location=_location(entry["node"]),
                    details={"has_ttl": bool(entry["has_ttl"])},
                )
            )

        if not ops and not topic_vars:
            warnings.append(
                "No Kafka consumer/producer/agent constructs found; the IR is empty."
            )

        return _build_result(
            source=source,
            ast_obj=module,
            ops=ops,
            source_topics=source_topics,
            sink_topics=sink_topics,
            app_id=app_id,
            has_backpressure=has_backpressure,
            warnings=warnings,
        )
    except Exception as exc:  # noqa: BLE001 - malformed-but-valid Python must not crash
        logger.exception("parse_kafka failed on structurally unusual Python input")
        raise ParseError(f"Failed to analyze Kafka client source: {exc}") from exc


# ---------------------------------------------------------------------------
# Java/Scala Kafka Streams DSL (line scanning — contract-sanctioned exception)
# ---------------------------------------------------------------------------

def _parse_java_streams(source: str, warnings: List[str]) -> ParseResult:
    """Regex line scan of a Kafka Streams (Java/Scala) topology."""
    try:
        lines = source.splitlines()
        ops: List[Operation] = []
        source_topics: List[str] = []
        sink_topics: List[str] = []
        app_id: Optional[str] = None
        has_backpressure = bool(_J_BACKPRESSURE.search(source))
        has_ttl = bool(_J_RETENTION.search(source))
        state_seen = False
        first_group_line: Optional[int] = None

        for lineno, line in enumerate(lines, start=1):
            if app_id is None:
                match = _J_APP_ID.search(line)
                if match:
                    app_id = match.group(1)

            for match in _J_SOURCE.finditer(line):
                topic = match.group(1)
                ops.append(
                    Operation(
                        type="SOURCE",
                        location=Location(line=lineno),
                        details={"topic": topic},
                    )
                )
                source_topics.append(topic)
            for match in _J_SINK.finditer(line):
                topic = match.group(1)
                ops.append(
                    Operation(
                        type="SINK",
                        location=Location(line=lineno),
                        details={"topic": topic},
                    )
                )
                sink_topics.append(topic)

            if _J_WINDOW.search(line):
                next_line = lines[lineno] if lineno < len(lines) else ""
                if "SessionWindows" in line:
                    kind = "session"
                elif "SlidingWindows" in line:
                    kind = "sliding"
                elif ".advanceBy" in line or ".advanceBy" in next_line:
                    kind = "hopping"
                else:
                    kind = "tumbling"
                duration = _J_DURATION.search(line)
                size_minutes: Optional[float] = None
                if duration:
                    size_minutes = int(duration.group(2)) * _DURATION_TO_MINUTES[
                        duration.group(1)
                    ]
                ops.append(
                    Operation(
                        type="WINDOW",
                        location=Location(line=lineno),
                        details={"kind": kind, "size_minutes": size_minutes},
                    )
                )

            if _J_STATE.search(line):
                state_seen = True
                ops.append(
                    Operation(
                        type="STATE",
                        location=Location(line=lineno),
                        details={"has_ttl": has_ttl},
                    )
                )
            elif first_group_line is None and _J_GROUP.search(line):
                first_group_line = lineno

        if not state_seen and first_group_line is not None:
            ops.append(
                Operation(
                    type="STATE",
                    location=Location(line=first_group_line),
                    details={"has_ttl": has_ttl},
                )
            )

        if not ops:
            warnings = warnings + [
                "No Kafka Streams DSL constructs recognized in the source."
            ]

        return _build_result(
            source=source,
            ast_obj=None,
            ops=ops,
            source_topics=source_topics,
            sink_topics=sink_topics,
            app_id=app_id,
            has_backpressure=has_backpressure,
            warnings=warnings,
        )
    except Exception as exc:  # noqa: BLE001 - a scan failure must surface cleanly
        logger.exception("parse_kafka failed while line-scanning Streams DSL input")
        raise ParseError(f"Failed to analyze Kafka Streams source: {exc}") from exc
