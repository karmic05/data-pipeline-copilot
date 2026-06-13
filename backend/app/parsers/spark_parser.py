"""Static AST parser for PySpark batch and Structured Streaming jobs.

``parse_spark`` analyzes Python source with the stdlib :mod:`ast` module only —
the job is never imported or executed. It walks DataFrame method chains
(``spark.read.format(...).load(...)``, ``df.write.mode(...).saveAsTable(...)``,
joins, aggregations, window specs, repartitions, caches, checkpoints,
watermarks, collects and broadcasts) and emits the unified IR consumed by the
rule engine, including read/write :class:`TableRef` entries and
read-table -> write-target dependency edges.
"""
from __future__ import annotations

import ast
import logging
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

#: Shorthand reader/writer methods that double as a format name.
_FORMAT_METHODS = frozenset({"parquet", "csv", "json", "orc", "avro", "text", "delta", "jdbc"})
_WRITE_TERMINALS = frozenset({"save", "saveAsTable", "insertInto", "start", "toTable"})
_BACKPRESSURE_OPTIONS = frozenset({"maxOffsetsPerTrigger", "maxFilesPerTrigger"})
_STREAM_ATTRS = frozenset({"readStream", "writeStream"})


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------

def _parse_python_module(source: str) -> ast.Module:
    """Parse Python ``source`` or raise :class:`ParseError` with the line number."""
    try:
        return ast.parse(source)
    except SyntaxError as exc:
        raise ParseError(
            f"Python syntax error in Spark job source: {exc.msg}", line=exc.lineno or 1
        ) from exc


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


def _int_literal(node: Optional[ast.AST]) -> Optional[int]:
    """An int literal value (bools excluded), or ``None``."""
    value = _literal(node)
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def _location(node: ast.AST) -> Location:
    """1-based source location for an AST node."""
    return Location(
        line=int(getattr(node, "lineno", 0) or 0),
        col=int(getattr(node, "col_offset", 0) or 0),
    )


def _segments(node: ast.AST) -> List[Tuple[str, Optional[ast.Call]]]:
    """Flatten a method chain into ``[(name, call_or_None), ...]`` root-first.

    ``spark.read.format("x").load("y")`` becomes
    ``[("spark", None), ("read", None), ("format", <Call>), ("load", <Call>)]``.
    """
    segs: List[Tuple[str, Optional[ast.Call]]] = []

    def rec(cur: ast.AST, pending: Optional[ast.Call]) -> None:
        if isinstance(cur, ast.Call):
            rec(cur.func, cur)
        elif isinstance(cur, ast.Attribute):
            rec(cur.value, None)
            segs.append((cur.attr, pending))
        elif isinstance(cur, ast.Name):
            segs.append((cur.id, pending))
        else:
            segs.append(("<expr>", pending))

    rec(node, None)
    return segs


def _seg_names(segs: List[Tuple[str, Optional[ast.Call]]]) -> List[str]:
    """Just the identifier sequence of a flattened chain."""
    return [name for name, _ in segs]


def _chain_str_arg(
    segs: List[Tuple[str, Optional[ast.Call]]], method: str
) -> Optional[str]:
    """First string argument of the named method anywhere in the chain."""
    for name, call in segs:
        if name == method and call is not None and call.args:
            value = _str_literal(call.args[0])
            if value is not None:
                return value
    return None


def _chain_option(
    segs: List[Tuple[str, Optional[ast.Call]]], key: str
) -> Optional[str]:
    """Value of ``.option(key, value)`` anywhere in the chain."""
    for name, call in segs:
        if name == "option" and call is not None and len(call.args) >= 2:
            if _str_literal(call.args[0]) == key:
                return _str_literal(call.args[1])
    return None


def _kwargs_map(call: Optional[ast.Call]) -> Dict[str, ast.AST]:
    """Keyword arguments of a call as ``{name: value_node}``."""
    if not isinstance(call, ast.Call):
        return {}
    return {kw.arg: kw.value for kw in call.keywords if kw.arg}


def _import_aliases(module: ast.Module, original: str) -> Set[str]:
    """All local names an imported symbol is bound to (handles ``as`` aliases)."""
    names: Set[str] = {original}
    for node in ast.walk(module):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == original and alias.asname:
                    names.add(alias.asname)
    return names


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def parse_spark(source: str) -> ParseResult:
    """Parse a PySpark job into the unified IR.

    Emits ``READ``/``WRITE``/``JOIN``/``GROUP_BY``/``AGGREGATE``/``WINDOW``/
    ``REPARTITION``/``CACHE``/``CHECKPOINT``/``WATERMARK``/``COLLECT``/
    ``BROADCAST`` operations, read/write table refs, read->write dependency
    edges, ``materialization.type="stream"`` for Structured Streaming jobs and
    ``extras["has_backpressure_config"]``.

    Raises:
        ParseError: when the source is not valid Python (includes line number).
    """
    module = _parse_python_module(source)
    try:
        warnings: List[str] = []
        ops: List[Operation] = []
        read_tables: List[str] = []
        write_tables: List[str] = []
        app_name: Optional[str] = None
        has_backpressure = False
        is_stream = False

        window_aliases = _import_aliases(module, "Window")
        broadcast_aliases = _import_aliases(module, "broadcast")

        # Variables (transitively) derived from a readStream chain, so that
        # ``collect()``/``toPandas()`` on them can be flagged as on_stream.
        stream_vars: Set[str] = set()
        assigns = sorted(
            (n for n in ast.walk(module) if isinstance(n, ast.Assign)),
            key=lambda n: (getattr(n, "lineno", 0), getattr(n, "col_offset", 0)),
        )
        for assign in assigns:
            names = _seg_names(_segments(assign.value))
            derived = "readStream" in names or (names and names[0] in stream_vars)
            if derived:
                for target in assign.targets:
                    if isinstance(target, ast.Name):
                        stream_vars.add(target.id)

        # Window spec chains are deduped per root ``Window`` name node so
        # ``Window.partitionBy(...).orderBy(...)`` emits a single WINDOW op.
        window_roots: Dict[int, Dict[str, Any]] = {}

        for node in ast.walk(module):
            if isinstance(node, ast.Attribute) and node.attr in _STREAM_ATTRS:
                is_stream = True
            if isinstance(node, ast.Constant) and node.value in _BACKPRESSURE_OPTIONS:
                has_backpressure = True
            if not isinstance(node, ast.Call):
                continue

            # broadcast(df) / F.broadcast(df)
            func = node.func
            if (isinstance(func, ast.Name) and func.id in broadcast_aliases) or (
                isinstance(func, ast.Attribute) and func.attr == "broadcast"
            ):
                ops.append(Operation(type="BROADCAST", location=_location(node)))
                continue
            if not isinstance(func, ast.Attribute):
                continue

            attr = func.attr
            kwargs = _kwargs_map(node)
            if any(k in _BACKPRESSURE_OPTIONS for k in kwargs):
                has_backpressure = True

            segs = _segments(node)
            names = _seg_names(segs)
            upstream = names[:-1]
            in_write = "write" in upstream or "writeStream" in upstream
            in_read = "read" in upstream or "readStream" in upstream

            if attr in _WRITE_TERMINALS or (attr in _FORMAT_METHODS and in_write):
                if attr == "start" and "writeStream" not in upstream:
                    continue  # unrelated .start() (e.g. threads)
                stream_write = "writeStream" in upstream
                target = _str_literal(node.args[0]) if node.args else None
                if target is None:
                    target = (
                        _str_literal(kwargs.get("path"))
                        or _str_literal(kwargs.get("name"))
                        or _chain_option(segs, "path")
                    )
                mode = (
                    _chain_str_arg(segs, "mode")
                    or _chain_str_arg(segs, "outputMode")
                    or _str_literal(kwargs.get("mode"))
                )
                fmt = _chain_str_arg(segs, "format") or (
                    attr if attr in _FORMAT_METHODS else None
                )
                ops.append(
                    Operation(
                        type="WRITE",
                        location=_location(node),
                        details={"mode": mode, "target": target, "format": fmt},
                    )
                )
                if stream_write:
                    is_stream = True
                if target:
                    write_tables.append(target)
            elif attr == "table" and not in_write:
                target = _str_literal(node.args[0]) if node.args else None
                ops.append(
                    Operation(
                        type="READ",
                        location=_location(node),
                        details={
                            "source": "readStream" if "readStream" in upstream else "table",
                            "format": "table",
                            "path_or_table": target,
                        },
                    )
                )
                if target:
                    read_tables.append(target)
            elif (attr == "load" or attr in _FORMAT_METHODS) and in_read:
                stream_read = "readStream" in upstream
                path = _str_literal(node.args[0]) if node.args else None
                if path is None:
                    path = _str_literal(kwargs.get("path")) or _chain_option(segs, "path")
                fmt = _chain_str_arg(segs, "format") or (
                    attr if attr in _FORMAT_METHODS else None
                )
                ops.append(
                    Operation(
                        type="READ",
                        location=_location(node),
                        details={
                            "source": "readStream" if stream_read else "read",
                            "format": fmt,
                            "path_or_table": path,
                        },
                    )
                )
                if stream_read:
                    is_stream = True
                if path:
                    read_tables.append(path)
            elif attr == "join":
                kind = None
                if len(node.args) >= 3:
                    kind = _str_literal(node.args[2])
                kind = kind or _str_literal(kwargs.get("how")) or "inner"
                has_on = len(node.args) >= 2 or "on" in kwargs
                ops.append(
                    Operation(
                        type="JOIN",
                        location=_location(node),
                        details={"kind": kind, "has_on_clause": has_on},
                    )
                )
            elif attr == "crossJoin":
                ops.append(
                    Operation(
                        type="JOIN",
                        location=_location(node),
                        details={"kind": "CROSS", "has_on_clause": False},
                    )
                )
            elif attr in {"groupBy", "groupby"}:
                columns = [v for v in (_str_literal(a) for a in node.args) if v]
                ops.append(
                    Operation(
                        type="GROUP_BY",
                        location=_location(node),
                        details={"columns": columns},
                    )
                )
            elif attr == "agg":
                ops.append(Operation(type="AGGREGATE", location=_location(node)))
            elif attr in {"repartition", "coalesce"}:
                ops.append(
                    Operation(
                        type="REPARTITION",
                        location=_location(node),
                        details={
                            "target_partitions": (
                                _int_literal(node.args[0]) if node.args else None
                            ),
                            "method": attr,
                        },
                    )
                )
            elif attr in {"cache", "persist"}:
                ops.append(
                    Operation(
                        type="CACHE",
                        location=_location(node),
                        details={"method": attr},
                    )
                )
            elif attr == "checkpoint":
                ops.append(Operation(type="CHECKPOINT", location=_location(node)))
            elif attr == "option" and node.args:
                key = _str_literal(node.args[0])
                if key == "checkpointLocation":
                    ops.append(Operation(type="CHECKPOINT", location=_location(node)))
                elif key in _BACKPRESSURE_OPTIONS:
                    has_backpressure = True
            elif attr == "options":
                if "checkpointLocation" in kwargs:
                    ops.append(Operation(type="CHECKPOINT", location=_location(node)))
            elif attr == "withWatermark":
                ops.append(
                    Operation(
                        type="WATERMARK",
                        location=_location(node),
                        details={
                            "column": _str_literal(node.args[0]) if node.args else None,
                            "delay": (
                                _str_literal(node.args[1]) if len(node.args) >= 2 else None
                            ),
                        },
                    )
                )
                is_stream = True
            elif attr in {"collect", "toPandas"}:
                on_stream = "readStream" in names or (
                    bool(names) and names[0] in stream_vars
                )
                ops.append(
                    Operation(
                        type="COLLECT",
                        location=_location(node),
                        details={"on_stream": on_stream, "method": attr},
                    )
                )
            elif attr == "appName" and node.args and app_name is None:
                app_name = _str_literal(node.args[0])

            # Window spec chains (Window.partitionBy / Window.orderBy ...).
            if names and names[0] in window_aliases and attr in {
                "partitionBy",
                "orderBy",
                "rowsBetween",
                "rangeBetween",
            }:
                root_node = _root_node_of(node)
                if root_node is not None:
                    entry = window_roots.setdefault(
                        id(root_node),
                        {"attrs": set(), "line": node.lineno, "col": node.col_offset},
                    )
                    entry["attrs"].update(n for n in names[1:])
                    entry["line"] = min(entry["line"], node.lineno)

        for entry in window_roots.values():
            ops.append(
                Operation(
                    type="WINDOW",
                    location=Location(line=entry["line"], col=entry["col"]),
                    details={"over_full_table": "partitionBy" not in entry["attrs"]},
                )
            )

        # Tables (deduped; reads + writes of the same name become readwrite).
        access: Dict[str, str] = {}
        for name in read_tables:
            access[name] = "readwrite" if access.get(name) == "write" else "read"
        for name in write_tables:
            access[name] = "readwrite" if access.get(name) == "read" else access.get(name, "write")
        tables = [TableRef(name=name, access_type=acc) for name, acc in access.items()]

        # Lineage: every read feeds every write target (best-effort dataflow).
        seen_edges: Set[Tuple[str, str]] = set()
        dependencies: List[Dependency] = []
        for src in dict.fromkeys(read_tables):
            for dst in dict.fromkeys(write_tables):
                if src != dst and (src, dst) not in seen_edges:
                    seen_edges.add((src, dst))
                    dependencies.append(
                        Dependency(source=src, target=dst, type="writes_to")
                    )

        ops.sort(key=lambda op: (op.location.line, op.location.col))
        ir = IR(
            format="spark",
            tables=tables,
            operations=ops,
            dependencies=dependencies,
            materialization=Materialization(type="stream" if is_stream else "unknown"),
            metadata=IRMetadata(name=app_name),
        )
        logger.debug(
            "parse_spark: %d op(s), %d table(s), stream=%s, backpressure=%s",
            len(ops),
            len(tables),
            is_stream,
            has_backpressure,
        )
        return ParseResult(
            ir=ir,
            source=source,
            ast=module,
            extras={"has_backpressure_config": has_backpressure},
            warnings=warnings,
        )
    except ParseError:
        raise
    except Exception as exc:  # noqa: BLE001 - malformed-but-valid Python must not crash
        logger.exception("parse_spark failed on structurally unusual input")
        raise ParseError(f"Failed to analyze Spark job source: {exc}") from exc


def _root_node_of(node: ast.AST) -> Optional[ast.Name]:
    """The root ``Name`` node of a method chain, used to dedupe Window specs."""
    cur: Optional[ast.AST] = node
    while cur is not None:
        if isinstance(cur, ast.Call):
            cur = cur.func
        elif isinstance(cur, ast.Attribute):
            cur = cur.value
        elif isinstance(cur, ast.Name):
            return cur
        else:
            return None
    return None
