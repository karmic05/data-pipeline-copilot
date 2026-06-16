"""SQL and Flink streaming-SQL parsers.

``parse_sql`` turns warehouse SQL (Snowflake / BigQuery / Postgres / Redshift /
Trino) into the unified :class:`app.schemas.ir.IR` using sqlglot for all
parsing. ``parse_flink`` handles Flink streaming SQL, tolerating Flink-only
DDL constructs (WATERMARK, connector ``WITH`` blocks, windowing TVFs) that
sqlglot cannot parse natively, and emits the streaming operation vocabulary
(SOURCE, SINK, WINDOW, STATE, WATERMARK, CHECKPOINT).

Both functions return :class:`app.schemas.ir.ParseResult` and raise
:class:`app.schemas.ir.ParseError` (with a 1-based line when known) on input
that cannot be parsed at all.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError as SqlglotParseError
from sqlglot.errors import SqlglotError

from app.schemas.ir import (
    IR,
    ColumnLineage,
    Dependency,
    Location,
    Operation,
    ParseError,
    ParseResult,
    TableRef,
)

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

_DIALECT_MAP: Dict[str, str] = {
    "snowflake": "snowflake",
    "bigquery": "bigquery",
    "postgres": "postgres",
    "postgresql": "postgres",
    "redshift": "redshift",
    "trino": "trino",
    "presto": "trino",
    "athena": "trino",
    "spark": "spark",
    "databricks": "databricks",
    "duckdb": "duckdb",
    "mysql": "mysql",
    "flink": "spark",
}

#: Column names that typically drive partition pruning in warehouses.
_PARTITIONISH_COLUMNS: Set[str] = {
    "date",
    "ds",
    "dt",
    "created_at",
    "event_date",
    "partition_date",
    "_partitiondate",
    "_table_suffix",
}

_CMP_OPS: Dict[type, str] = {
    exp.EQ: "=",
    exp.NEQ: "!=",
    exp.GT: ">",
    exp.GTE: ">=",
    exp.LT: "<",
    exp.LTE: "<=",
}

_DATE_LITERAL_RX = re.compile(r"^\d{4}-\d{2}-\d{2}")
_INTERVAL_RX = re.compile(r"INTERVAL\s*'([\d.]+)'\s*(\w+)", re.IGNORECASE)
_WINDOW_FN_RX = re.compile(r"\b(TUMBLE|HOP|SESSION|CUMULATE)\s*\(", re.IGNORECASE)
_TVF_RX = re.compile(r"TABLE\s*\(\s*(?:TUMBLE|HOP|SESSION|CUMULATE)\s*\(", re.IGNORECASE)
_WATERMARK_RX = re.compile(r"WATERMARK\s+FOR\s+([`\"\w]+)\s+AS\s+(.+)", re.IGNORECASE)
_CREATE_TABLE_RX = re.compile(
    r"CREATE\s+(?:TEMPORARY\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([`\"\w.]+)",
    re.IGNORECASE,
)
_CONNECTOR_RX = re.compile(r"'connector'\s*=\s*'([^']+)'", re.IGNORECASE)
_TOPIC_RX = re.compile(r"'topic'\s*=\s*'([^']+)'", re.IGNORECASE)
_SET_KV_RX = re.compile(r"'([^']+)'\s*=\s*'([^']*)'")

_WINDOW_KIND_MAP: Dict[str, str] = {
    "tumble": "tumbling",
    "hop": "hopping",
    "session": "session",
    "cumulate": "cumulative",
}

_UNIT_MINUTES: Dict[str, float] = {
    "millisecond": 1 / 60_000,
    "milliseconds": 1 / 60_000,
    "second": 1 / 60,
    "seconds": 1 / 60,
    "minute": 1.0,
    "minutes": 1.0,
    "hour": 60.0,
    "hours": 60.0,
    "day": 1440.0,
    "days": 1440.0,
}

_BACKPRESSURE_TOKENS = (
    "backpressure",
    "buffer-timeout",
    "taskmanager.network",
    "taskmanager.memory.network",
)

_WRITE_STATEMENTS = (exp.Create, exp.Insert, exp.Update, exp.Delete, exp.Merge)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _normalize_dialect(dialect: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Map a user/detected dialect to a sqlglot ``read`` name.

    Returns ``(sqlglot_read_dialect, warning)``; unknown dialects fall back to
    the generic sqlglot dialect with a warning instead of failing.
    """
    if not dialect:
        return None, None
    key = dialect.strip().lower()
    if key in _DIALECT_MAP:
        return _DIALECT_MAP[key], None
    return None, f"Unknown SQL dialect '{dialect}' - parsed with the generic dialect."


def _meta_loc(node: Optional[exp.Expression]) -> Optional[Tuple[int, int]]:
    """Best (earliest) ``(line, col)`` carried by sqlglot token metadata.

    sqlglot >= 26 records token positions on expressions (``_meta`` /
    ``meta``); older versions kept them in ``args["meta"]``. Both are read.
    """
    if node is None:
        return None
    best: Optional[Tuple[int, int]] = None
    try:
        for n in node.walk():
            meta = getattr(n, "_meta", None)
            if not isinstance(meta, dict):
                meta = n.args.get("meta")
            if isinstance(meta, dict):
                line = meta.get("line")
                if isinstance(line, int) and line > 0:
                    cand = (line, int(meta.get("col") or 0))
                    if best is None or cand < best:
                        best = cand
    except Exception:  # pragma: no cover - defensive against exotic nodes
        return best
    return best


def _interval_minutes(value: str, unit: str) -> float:
    """Convert an ``INTERVAL '<value>' <unit>`` literal to minutes."""
    try:
        return round(float(value) * _UNIT_MINUTES.get(unit.lower(), 1.0), 4)
    except (TypeError, ValueError):
        return 0.0


def _strip_quotes(name: str) -> str:
    return name.strip().strip('`"')


def _matching_paren(text: str, open_idx: int) -> Optional[int]:
    """Index of the ``)`` matching ``text[open_idx] == '('`` (string-aware)."""
    depth = 0
    i = open_idx
    in_string = False
    while i < len(text):
        ch = text[i]
        if in_string:
            if ch == "'":
                in_string = False
        elif ch == "'":
            in_string = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _blank_span(text: str, start: int, end: int, replacement: str = "") -> str:
    """Blank ``text[start:end]`` with spaces, preserving newlines (and lines).

    ``replacement`` is written into the first non-newline positions so the
    overall character/line layout stays stable for line-number reporting.
    """
    chars = [c if c == "\n" else " " for c in text[start:end]]
    j = 0
    for ch in replacement:
        while j < len(chars) and chars[j] == "\n":
            j += 1
        if j >= len(chars):
            break
        chars[j] = ch
        j += 1
    return text[:start] + "".join(chars) + text[end:]


def _raise_parse_error(err: Exception) -> None:
    """Translate a sqlglot exception into the app-level :class:`ParseError`."""
    line: Optional[int] = None
    message = str(err).splitlines()[0] if str(err) else "SQL could not be parsed."
    if isinstance(err, SqlglotParseError):
        errors = getattr(err, "errors", None) or []
        if errors:
            first = errors[0]
            desc = first.get("description") or message
            highlight = first.get("highlight")
            desc = re.sub(r"<Token[^>]*>", f"'{highlight}'" if highlight else "input", desc)
            message = desc
            line = first.get("line")
    if line is None:
        m = re.search(r"[Ll]ine[:\s]+(\d+)", str(err))
        if m:
            line = int(m.group(1))
    raise ParseError(f"SQL parse error: {message}", line=line) from err


# --------------------------------------------------------------------------- #
# IR builder
# --------------------------------------------------------------------------- #


class _IRBuilder:
    """Accumulates tables / operations / dependencies / lineage into an IR.

    One builder instance handles a whole source (possibly multiple
    statements); :meth:`add_statement` is called per parsed statement and
    :meth:`finalize` resolves access types, dependency edges and (for
    streaming) SOURCE/SINK operations.
    """

    def __init__(
        self,
        ir: IR,
        source: str,
        read_dialect: Optional[str],
        *,
        streaming: bool = False,
    ) -> None:
        self.ir = ir
        self.source = source
        self.read = read_dialect
        self.streaming = streaming
        self.warnings: List[str] = []

        self._tables: Dict[str, TableRef] = {}
        self._read_names: Set[str] = set()
        self._written_names: Set[str] = set()
        self._written_order: List[str] = []
        self._deps: List[Dependency] = []
        self._dep_seen: Set[Tuple[str, str, str]] = set()
        self._connectors: Dict[str, Dict[str, Any]] = {}

        # Per-statement state.
        self._alias_map: Dict[str, str] = {}
        self._offset = 0
        self._stmt_text = source
        self._stmt_has_window = False
        self._single_read: Optional[str] = None

    # -- generic helpers ---------------------------------------------------- #

    def _sql(self, node: Optional[exp.Expression]) -> str:
        """Render a node back to SQL in the active dialect (never raises)."""
        if node is None:
            return ""
        try:
            return node.sql(dialect=self.read)
        except Exception:  # pragma: no cover - sqlglot generation edge cases
            return str(node)

    def _loc(self, node: Optional[exp.Expression], pattern: Optional[str] = None) -> Location:
        """Location from token metadata, else by scanning the statement text."""
        got = _meta_loc(node)
        if got:
            return Location(line=got[0] + self._offset, col=got[1])
        return self._scan_loc(pattern)

    def _loc_nodes(self, nodes: Sequence[Optional[exp.Expression]]) -> Optional[Location]:
        """Earliest metadata location across several nodes, or ``None``."""
        best: Optional[Tuple[int, int]] = None
        for node in nodes:
            got = _meta_loc(node)
            if got and (best is None or got < best):
                best = got
        if best:
            return Location(line=best[0] + self._offset, col=best[1])
        return None

    def _scan_loc(self, pattern: Optional[str]) -> Location:
        """Fallback location by regex-scanning the current statement text."""
        if pattern:
            rx = re.compile(pattern, re.IGNORECASE)
            for i, line_text in enumerate(self._stmt_text.splitlines(), 1):
                m = rx.search(line_text)
                if m:
                    return Location(line=i + self._offset, col=m.start() + 1)
        return Location()

    def add_operation(
        self, op_type: str, location: Location, details: Dict[str, Any]
    ) -> None:
        """Append an operation to the IR."""
        self.ir.operations.append(
            Operation(type=op_type, location=location, details=details)
        )

    def _add_dep(self, source: str, target: str, dep_type: str) -> None:
        key = (source.lower(), target.lower(), dep_type)
        if source.lower() == target.lower() or key in self._dep_seen:
            return
        self._dep_seen.add(key)
        self._deps.append(Dependency(source=source, target=target, type=dep_type))  # type: ignore[arg-type]

    # -- table registry ----------------------------------------------------- #

    def _register_table(
        self,
        t: exp.Table,
        *,
        read: bool = False,
        write: bool = False,
    ) -> Optional[str]:
        """Register a sqlglot Table node; returns its display name."""
        if not isinstance(t.this, exp.Identifier):
            return None
        parts = [p for p in (t.catalog, t.db, t.name) if p]
        if not parts:
            return None
        display = ".".join(parts)
        key = display.lower()
        ref = self._tables.get(key)
        if ref is None:
            ref = TableRef(
                name=display,
                alias=t.alias or None,
                schema_name=t.db or None,
                database=t.catalog or None,
            )
            self._tables[key] = ref
        elif t.alias and not ref.alias:
            ref.alias = t.alias
        if read:
            self._read_names.add(key)
        if write:
            self._written_names.add(key)
            if display not in self._written_order:
                self._written_order.append(display)
        if t.alias:
            self._alias_map[t.alias.lower()] = display
        self._alias_map[t.name.lower()] = display
        self._alias_map[key] = display
        return display

    def register_raw_table(self, raw_name: str, *, write: bool = False) -> Optional[str]:
        """Register a table from a raw (possibly quoted, dotted) name string."""
        name = _strip_quotes(raw_name)
        if not name:
            return None
        parts = name.split(".")
        display = ".".join(parts)
        key = display.lower()
        if key not in self._tables:
            self._tables[key] = TableRef(
                name=display,
                schema_name=parts[-2] if len(parts) >= 2 else None,
                database=parts[-3] if len(parts) >= 3 else None,
            )
        if write:
            self._written_names.add(key)
            if display not in self._written_order:
                self._written_order.append(display)
        return display

    def register_connector(
        self, table_name: str, connector: str, topic: Optional[str], location: Location
    ) -> None:
        """Remember a Flink connector table for SOURCE/SINK emission."""
        display = self.register_raw_table(table_name)
        if display is None:
            return
        self._connectors.setdefault(
            display.lower(),
            {"table": display, "connector": connector, "topic": topic, "loc": location},
        )

    def _resolve(self, qualifier: str) -> Optional[str]:
        return self._alias_map.get(qualifier.lower()) if qualifier else None

    # -- statement processing ------------------------------------------------ #

    def add_statement(
        self,
        stmt: exp.Expression,
        *,
        line_offset: int = 0,
        stmt_text: Optional[str] = None,
        stmt_has_window: bool = False,
    ) -> None:
        """Analyze one parsed statement and fold its facts into the IR."""
        self._offset = line_offset
        self._stmt_text = stmt_text if stmt_text is not None else self.source
        self._stmt_has_window = stmt_has_window
        self._alias_map = {}

        if not isinstance(stmt, _WRITE_STATEMENTS + (exp.Select, exp.Union, exp.Subquery)):
            logger.debug("Skipping unsupported statement type %s", type(stmt).__name__)
            return
        if isinstance(stmt, exp.Subquery):
            inner = stmt.this
            if not isinstance(inner, (exp.Select, exp.Union)):
                return
            stmt = inner

        write_target: Optional[str] = None
        root_query: Optional[exp.Expression] = None
        write_nodes: List[exp.Table] = []
        cte_names = {
            cte.alias.lower() for cte in stmt.find_all(exp.CTE) if cte.alias
        }

        # ---- statement-kind handling ------------------------------------- #
        if isinstance(stmt, exp.Create):
            target = self._target_table(stmt.this)
            root_query = stmt.args.get("expression")
            if target is not None:
                write_nodes.append(target)
                is_pure_ddl = root_query is None
                write_target = self._register_table(
                    target, write=not (self.streaming and is_pure_ddl)
                )
            self._apply_materialization(stmt)
        elif isinstance(stmt, exp.Insert):
            target = self._target_table(stmt.this)
            root_query = stmt.args.get("expression")
            columns: List[str] = []
            if isinstance(stmt.this, exp.Schema):
                columns = [
                    ident.name
                    for ident in stmt.this.expressions
                    if isinstance(ident, (exp.Identifier, exp.Column))
                ]
            if target is not None:
                write_nodes.append(target)
                write_target = self._register_table(target, write=True)
            self.add_operation(
                "INSERT",
                self._loc(stmt.this, r"\binsert\b"),
                {
                    "target": write_target or "",
                    "columns": columns,
                    "overwrite": bool(stmt.args.get("overwrite")),
                },
            )
        elif isinstance(stmt, exp.Update):
            target = self._target_table(stmt.this)
            if target is not None:
                write_nodes.append(target)
                write_target = self._register_table(target, write=True)
            set_columns = [
                self._sql(eq.this)
                for eq in stmt.expressions
                if isinstance(eq, exp.EQ)
            ]
            where = stmt.args.get("where")
            self.add_operation(
                "UPDATE",
                self._loc(stmt.this, r"\bupdate\b"),
                {
                    "target": write_target or "",
                    "columns": set_columns,
                    "has_where": where is not None,
                },
            )
            if where is not None:
                self._emit_filter(where.this)
        elif isinstance(stmt, exp.Delete):
            target = self._target_table(stmt.this)
            if target is not None:
                write_nodes.append(target)
                write_target = self._register_table(target, write=True)
            where = stmt.args.get("where")
            self.add_operation(
                "DELETE",
                self._loc(stmt.this, r"\bdelete\b"),
                {"target": write_target or "", "has_where": where is not None},
            )
            if where is not None:
                self._emit_filter(where.this)
        elif isinstance(stmt, exp.Merge):
            target = self._target_table(stmt.this)
            if target is not None:
                write_nodes.append(target)
                write_target = self._register_table(target, write=True)
            using = stmt.args.get("using")
            using_name = ""
            if isinstance(using, exp.Table):
                using_name = self._register_table(using, read=True) or ""
            elif using is not None:
                using_name = self._sql(using)
            on_cond = stmt.args.get("on")
            self.add_operation(
                "MERGE",
                self._loc(stmt.this, r"\bmerge\b"),
                {
                    "target": write_target or "",
                    "source": using_name,
                    "condition_sql": self._sql(on_cond),
                },
            )
        else:  # Select / Union root
            root_query = stmt

        # ---- CTE operations ----------------------------------------------- #
        all_tables = list(stmt.find_all(exp.Table))
        for cte in stmt.find_all(exp.CTE):
            name = cte.alias
            if not name:
                continue
            refs = sum(
                1
                for t in all_tables
                if t.name.lower() == name.lower() and not t.db
            )
            self.add_operation(
                "CTE",
                self._loc(cte, rf"\b{re.escape(name)}\b"),
                {"name": name, "reference_count": refs},
            )
            self._alias_map.setdefault(name.lower(), name)

        # ---- table registration ------------------------------------------- #
        write_ids = {id(n) for n in write_nodes}
        stmt_reads: List[str] = []
        for t in all_tables:
            if id(t) in write_ids:
                continue
            if not isinstance(t.this, exp.Identifier):
                continue
            if t.name.lower() in cte_names and not t.db:
                if t.alias:
                    self._alias_map[t.alias.lower()] = t.name
                continue
            display = self._register_table(t, read=True)
            if display and display not in stmt_reads:
                stmt_reads.append(display)
        for sub in stmt.find_all(exp.Subquery):
            if sub.alias:
                self._alias_map.setdefault(sub.alias.lower(), sub.alias)
        self._single_read = stmt_reads[0] if len(stmt_reads) == 1 else None

        # ---- per-select operations ----------------------------------------- #
        root_select = self._root_select(root_query)
        for sel in stmt.find_all(exp.Select):
            self._process_select(sel)

        # ---- set operations / subqueries ------------------------------------ #
        for union in stmt.find_all(exp.Union):
            self.add_operation(
                "UNION",
                self._loc(union.args.get("expression"), r"\bunion\b"),
                {"all": not union.args.get("distinct")},
            )
            self._emit_order_and_limit(union)
        self._emit_subquery_ops(stmt)

        # ---- columns / dependencies / lineage ------------------------------- #
        self._collect_columns(stmt, cte_names)
        target_name = write_target or "query_result"
        for rd in stmt_reads:
            self._add_dep(rd, target_name, "reads_from")
        if write_target:
            self._add_dep("query_result", write_target, "writes_to")
        if root_select is not None:
            self._collect_lineage(root_select, write_target or "query_result")

    # -- statement-kind helpers ------------------------------------------------ #

    @staticmethod
    def _target_table(node: Optional[exp.Expression]) -> Optional[exp.Table]:
        """Unwrap a write-target arg (Schema/Table/Alias) to its Table node."""
        if isinstance(node, exp.Schema):
            node = node.this
        if isinstance(node, exp.Alias):
            node = node.this
        if isinstance(node, exp.Table):
            return node
        return None

    @staticmethod
    def _root_select(query: Optional[exp.Expression]) -> Optional[exp.Select]:
        """Left-most SELECT of a statement's root query (for lineage)."""
        seen = 0
        while query is not None and seen < 20:
            seen += 1
            if isinstance(query, exp.Select):
                return query
            if isinstance(query, exp.Subquery):
                query = query.this
            elif isinstance(query, exp.Union):
                query = query.left
            else:
                return None
        return None

    def _apply_materialization(self, create: exp.Create) -> None:
        """Record materialization type + PARTITION BY / CLUSTER BY columns."""
        mat = self.ir.materialization
        kind = (create.args.get("kind") or "").upper()
        partition: List[str] = []
        cluster: List[str] = []
        is_materialized = False
        props = create.args.get("properties")
        if props is not None:
            for prop in props.expressions:
                cls_name = type(prop).__name__
                if "Materialized" in cls_name:
                    is_materialized = True
                elif "Partition" in cls_name:
                    partition.extend(self._property_columns(prop))
                elif "Cluster" in cls_name:
                    cluster.extend(self._property_columns(prop))
        if not self.streaming:
            if kind == "TABLE":
                mat.type = "table"
            elif kind == "VIEW":
                mat.type = "view"
            if is_materialized:
                mat.strategy = "materialized"
        for col in partition:
            if col not in mat.partition_by:
                mat.partition_by.append(col)
        for col in cluster:
            if col not in mat.cluster_by:
                mat.cluster_by.append(col)

    def _property_columns(self, prop: exp.Expression) -> List[str]:
        """Base column names referenced by a PARTITION/CLUSTER property."""
        cols = [c.name for c in prop.find_all(exp.Column) if c.name]
        if cols:
            return cols
        idents = [i.name for i in prop.find_all(exp.Identifier) if i.name]
        if idents:
            return idents
        inner = prop.args.get("this")
        return [self._sql(inner)] if inner is not None else []

    # -- select-scope processing ------------------------------------------------ #

    def _scope_entries(self, sel: exp.Select) -> List[Tuple[str, Optional[str]]]:
        """(display_name, alias) for the FROM table and each JOIN target."""
        entries: List[Tuple[str, Optional[str]]] = []
        frm = sel.args.get("from") or sel.args.get("from_")
        sources: List[exp.Expression] = []
        if frm is not None and frm.this is not None:
            sources.append(frm.this)
        for join in sel.args.get("joins") or []:
            if join.this is not None:
                sources.append(join.this)
        for src in sources:
            if isinstance(src, exp.Table) and isinstance(src.this, exp.Identifier):
                parts = [p for p in (src.catalog, src.db, src.name) if p]
                display = self._resolve(src.alias or src.name) or ".".join(parts)
                entries.append((display, src.alias or None))
            elif isinstance(src, exp.Subquery):
                alias = src.alias or "subquery"
                entries.append((alias, src.alias or None))
            else:
                entries.append((self._sql(src)[:60] or "unknown", None))
        return entries

    def _process_select(self, sel: exp.Select) -> None:
        """Emit SELECT / JOIN / FILTER / AGGREGATE / WINDOW / ORDER_BY /
        DISTINCT / LIMIT (and STATE on streams) for one select scope."""
        entries = self._scope_entries(sel)
        scope_names = [e[0] for e in entries]

        # SELECT
        projections = [self._sql(p) for p in sel.expressions]
        has_star = False
        star_tables: List[str] = []
        for p in sel.expressions:
            if isinstance(p, exp.Star):
                has_star = True
                star_tables.extend(n for n in scope_names if n not in star_tables)
            elif isinstance(p, exp.Column) and isinstance(p.this, exp.Star):
                has_star = True
                resolved = self._resolve(p.table) or p.table
                if resolved and resolved not in star_tables:
                    star_tables.append(resolved)
        # ``columns`` / ``star`` / ``tables`` mirror the contract keys for
        # consumers (cost / security engines) that read those spellings.
        self.add_operation(
            "SELECT",
            self._loc_nodes(sel.expressions) or self._loc(sel, r"\bselect\b"),
            {
                "projections": projections,
                "has_star": has_star,
                "star_tables": star_tables,
                "columns": projections,
                "star": has_star,
                "tables": star_tables,
            },
        )

        # JOINs
        prev_name = scope_names[0] if scope_names else ""
        joins: Sequence[exp.Join] = sel.args.get("joins") or []
        for idx, join in enumerate(joins):
            right_name = scope_names[idx + 1] if idx + 1 < len(scope_names) else ""
            self._emit_join(join, prev_name, right_name)
            prev_name = right_name or prev_name

        # FILTER (+ PARTITION_FILTER)
        where = sel.args.get("where")
        if where is not None and where.this is not None:
            self._emit_filter(where.this)

        # AGGREGATE / STATE
        group = sel.args.get("group")
        aggs = [
            f
            for f in sel.find_all(exp.AggFunc)
            if f.find_ancestor(exp.Select) is sel and f.find_ancestor(exp.Window) is None
        ]
        if group is not None or aggs:
            group_exprs = (
                [self._sql(e) for e in group.expressions] if group is not None else []
            )
            agg_loc = self._loc(group if group is not None else (aggs[0] if aggs else sel),
                                r"\bgroup\s+by\b")
            self.add_operation(
                "AGGREGATE",
                agg_loc,
                {"group_by": group_exprs, "functions": [self._sql(f) for f in aggs]},
            )
            if self.streaming and not self._stmt_has_window:
                self.add_operation(
                    "STATE",
                    agg_loc,
                    {"has_ttl": "table.exec.state.ttl" in self.source.lower()},
                )

        # WINDOW (OVER ...) - batch SQL only; streaming windows come from
        # TUMBLE/HOP/SESSION/CUMULATE fingerprints with {kind, size_minutes}.
        if not self.streaming:
            for win in sel.find_all(exp.Window):
                if win.find_ancestor(exp.Select) is not sel:
                    continue
                self._emit_window(win)

        # ORDER_BY / LIMIT / DISTINCT
        self._emit_order_and_limit(sel)
        if sel.args.get("distinct") is not None:
            details: Dict[str, Any] = {}
            if self.streaming:
                details["on_stream"] = True
            self.add_operation("DISTINCT", self._loc(None, r"\bdistinct\b"), details)

    def _emit_join(self, join: exp.Join, left_name: str, right_name: str) -> None:
        on = join.args.get("on")
        using = [u.name for u in join.args.get("using") or [] if hasattr(u, "name")]
        side = (join.side or "").upper()
        kind = (join.kind or "").upper()
        if kind == "CROSS":
            join_kind = "CROSS"
        elif side in {"LEFT", "RIGHT", "FULL"}:
            join_kind = side
        elif kind:
            join_kind = kind
        elif on is None and not using:
            # Comma joins / bare JOINs without a condition are cross products.
            join_kind = "CROSS"
        else:
            join_kind = "INNER"
        self.add_operation(
            "JOIN",
            self._loc(join, r"\bjoin\b|,"),
            {
                "kind": join_kind,
                "has_on_clause": on is not None,
                "using": using,
                "condition_sql": self._sql(on) if on is not None else "",
                "left": left_name,
                "right": right_name,
                "implicit_cast": self._join_has_implicit_cast(on),
            },
        )

    @staticmethod
    def _join_has_implicit_cast(on: Optional[exp.Expression]) -> bool:
        """True when the ON condition casts or function-wraps a column."""
        if on is None:
            return False
        for node in on.walk():
            if isinstance(node, exp.Cast) and node.find(exp.Column) is not None:
                return True
            if type(node) in _CMP_OPS:
                for side in (node.this, node.args.get("expression")):
                    if (
                        isinstance(side, exp.Func)
                        and not isinstance(side, exp.Cast)
                        and side.find(exp.Column) is not None
                    ):
                        return True
        return False

    def _emit_window(self, win: exp.Window) -> None:
        fn = win.this
        fn_sql = self._sql(fn)
        partition_by = [self._sql(e) for e in win.args.get("partition_by") or []]
        order = win.args.get("order")
        order_by = [self._sql(e) for e in order.expressions] if order is not None else []
        self.add_operation(
            "WINDOW",
            self._loc(win, r"\bover\s*\("),
            {
                "function": fn_sql.split("(", 1)[0].strip().upper() if fn_sql else "",
                "partition_by": partition_by,
                "order_by": order_by,
                "has_frame": win.args.get("spec") is not None,
                "over_full_table": not partition_by,
            },
        )

    def _emit_order_and_limit(self, node: exp.Expression) -> None:
        """ORDER_BY and LIMIT ops for a Select or Union node."""
        order = node.args.get("order")
        limit = node.args.get("limit")
        if order is not None:
            details: Dict[str, Any] = {
                "has_limit": limit is not None,
                "expressions": [self._sql(e) for e in order.expressions],
            }
            if self.streaming:
                details["on_stream"] = True
            self.add_operation("ORDER_BY", self._loc(order, r"\border\s+by\b"), details)
        if limit is not None:
            value: Optional[int] = None
            lit = limit.args.get("expression")
            if isinstance(lit, exp.Literal):
                try:
                    value = int(lit.name)
                except (TypeError, ValueError):
                    value = None
            self.add_operation(
                "LIMIT", self._loc(limit, r"\blimit\b"), {"value": value}
            )

    # -- WHERE analysis ------------------------------------------------------- #

    def _emit_filter(self, cond: exp.Expression) -> None:
        """One FILTER op per WHERE clause, plus PARTITION_FILTER ops."""
        predicates: List[Dict[str, Any]] = []
        partition_hits: List[Dict[str, Any]] = []
        function_wrapped: List[str] = []
        leading_wildcard = False
        partitionish = _PARTITIONISH_COLUMNS | {
            c.lower() for c in self.ir.materialization.partition_by
        }

        def side_info(side: Optional[exp.Expression]) -> Tuple[str, str, List[str]]:
            """(role, rendered, base_columns) for one comparison side."""
            if side is None:
                return "value", "", []
            if isinstance(side, exp.Column) and not isinstance(side.this, exp.Star):
                return "column", self._sql(side), [side.name]
            if isinstance(side, exp.Func):
                inner_cols = [
                    c.name
                    for c in side.find_all(exp.Column)
                    if not isinstance(c.this, exp.Star)
                ]
                if inner_cols:
                    return "func", self._sql(side), inner_cols
            return "value", self._sql(side), []

        def record(column: str, op: str, value: Any, base_cols: List[str]) -> None:
            predicates.append({"column": column, "op": op, "value": value})
            for base in base_cols:
                if base.split(".")[-1].lower() in partitionish:
                    partition_hits.append(
                        {"column": base, "op": op, "value": value}
                    )

        for node in cond.walk():
            node_type = type(node)
            if node_type in _CMP_OPS:
                left, right = node.this, node.args.get("expression")
                l_role, l_repr, l_cols = side_info(left)
                r_role, r_repr, r_cols = side_info(right)
                if l_role == "func":
                    function_wrapped.extend(c for c in l_cols if c not in function_wrapped)
                if r_role == "func":
                    function_wrapped.extend(c for c in r_cols if c not in function_wrapped)
                if l_role in ("column", "func"):
                    record(l_repr, _CMP_OPS[node_type], r_repr, l_cols)
                elif r_role in ("column", "func"):
                    record(r_repr, _CMP_OPS[node_type], l_repr, r_cols)
                else:
                    record(l_repr, _CMP_OPS[node_type], r_repr, [])
            elif isinstance(node, (exp.Like, exp.ILike)):
                op = "ILIKE" if isinstance(node, exp.ILike) else "LIKE"
                if isinstance(node.parent, exp.Not):
                    op = "NOT " + op
                _, l_repr, l_cols = side_info(node.this)
                pattern = node.args.get("expression")
                pattern_repr = self._sql(pattern)
                if (
                    isinstance(pattern, exp.Literal)
                    and pattern.is_string
                    and pattern.name.startswith("%")
                ):
                    leading_wildcard = True
                record(l_repr, op, pattern_repr, l_cols)
            elif isinstance(node, exp.In):
                op = "NOT IN" if isinstance(node.parent, exp.Not) else "IN"
                _, l_repr, l_cols = side_info(node.this)
                if node.args.get("query") is not None:
                    value: Any = "(subquery)"
                else:
                    value = [self._sql(e) for e in node.expressions][:25]
                record(l_repr, op, value, l_cols)
            elif isinstance(node, exp.Between):
                _, l_repr, l_cols = side_info(node.this)
                low = self._sql(node.args.get("low"))
                high = self._sql(node.args.get("high"))
                record(l_repr, "BETWEEN", f"{low} AND {high}", l_cols)
            elif isinstance(node, exp.Is):
                op = "IS NOT" if isinstance(node.parent, exp.Not) else "IS"
                _, l_repr, l_cols = side_info(node.this)
                record(l_repr, op, self._sql(node.args.get("expression")), l_cols)

        hardcoded_dates: List[str] = []
        for lit in cond.find_all(exp.Literal):
            if lit.is_string and _DATE_LITERAL_RX.match(lit.name):
                if lit.name not in hardcoded_dates:
                    hardcoded_dates.append(lit.name)

        not_in_subquery = any(
            isinstance(n, exp.Not)
            and isinstance(n.this, exp.In)
            and n.this.args.get("query") is not None
            for n in cond.walk()
        )

        loc = self._loc(cond, r"\bwhere\b")
        self.add_operation(
            "FILTER",
            loc,
            {
                "predicates": predicates,
                "has_or": any(isinstance(n, exp.Or) for n in cond.walk()),
                "not_in_subquery": not_in_subquery,
                "leading_wildcard_like": leading_wildcard,
                "hardcoded_dates": hardcoded_dates,
                "function_wrapped_columns": function_wrapped,
            },
        )
        seen_parts: Set[Tuple[str, str]] = set()
        for hit in partition_hits:
            key = (hit["column"].lower(), hit["op"])
            if key in seen_parts:
                continue
            seen_parts.add(key)
            self.add_operation("PARTITION_FILTER", loc, hit)

    # -- subqueries ------------------------------------------------------------ #

    def _emit_subquery_ops(self, stmt: exp.Expression) -> None:
        """SUBQUERY ops with clause (select/where/from) + correlation flag."""
        for sub in stmt.find_all(exp.Subquery):
            inner = sub.this
            if not isinstance(inner, (exp.Select, exp.Union)):
                continue
            anc = sub.find_ancestor(exp.From, exp.Join, exp.Where)
            if isinstance(anc, (exp.From, exp.Join)):
                clause = "from"
            elif isinstance(anc, exp.Where):
                clause = "where"
            else:
                clause = "select"
            self.add_operation(
                "SUBQUERY",
                self._loc(sub, r"\(\s*select\b"),
                {"clause": clause, "correlated": self._is_correlated(sub)},
            )
        for exists in stmt.find_all(exp.Exists):
            inner = exists.this
            if not isinstance(inner, (exp.Select, exp.Union)):
                continue
            self.add_operation(
                "SUBQUERY",
                self._loc(exists, r"\bexists\b"),
                {"clause": "where", "correlated": self._is_correlated(inner)},
            )

    def _is_correlated(self, q: exp.Expression) -> bool:
        """True when the subquery references an alias from an outer scope."""
        inner_names: Set[str] = set()
        for t in q.find_all(exp.Table):
            inner_names.add(t.name.lower())
            if t.alias:
                inner_names.add(t.alias.lower())
        for s in q.find_all(exp.Subquery):
            if s.alias:
                inner_names.add(s.alias.lower())
        for c in q.find_all(exp.Column):
            qual = (c.table or "").lower()
            if qual and qual not in inner_names and qual in self._alias_map:
                return True
        return False

    # -- columns & lineage ------------------------------------------------------ #

    def _collect_columns(self, stmt: exp.Expression, cte_names: Set[str]) -> None:
        """Best-effort referenced-column attribution per table."""
        for c in stmt.find_all(exp.Column):
            if isinstance(c.this, exp.Star) or not c.name:
                continue
            qual = (c.table or "").lower()
            if qual:
                display = self._resolve(qual)
            else:
                display = self._single_read
            if not display or display.lower() in cte_names:
                continue
            ref = self._tables.get(display.lower())
            if ref is not None and c.name not in ref.columns:
                ref.columns.append(c.name)

    def _collect_lineage(self, sel: exp.Select, output_table: str) -> None:
        """Column lineage rows from the root select's projections."""
        for proj in sel.expressions:
            if isinstance(proj, exp.Star):
                continue
            if isinstance(proj, exp.Column) and isinstance(proj.this, exp.Star):
                continue
            out_col = proj.alias_or_name or self._sql(proj)
            inner = proj.this if isinstance(proj, exp.Alias) else proj
            if inner is None:
                continue
            if inner.find(exp.Window) is not None:
                transformation = "window"
            elif inner.find(exp.AggFunc) is not None:
                transformation = "aggregation"
            elif isinstance(inner, exp.Column):
                transformation = "direct"
            else:
                transformation = "expression"
            source_cols = [
                c
                for c in inner.find_all(exp.Column)
                if not isinstance(c.this, exp.Star) and c.name
            ]
            if not source_cols and isinstance(inner, exp.Column) and inner.name:
                source_cols = [inner]
            for src in source_cols:
                qual = (src.table or "").lower()
                source_table = (
                    self._resolve(qual)
                    or (src.table or None)
                    or self._single_read
                    or "unknown"
                )
                self.ir.column_lineage.append(
                    ColumnLineage(
                        output_table=output_table,
                        output_column=out_col,
                        source_table=source_table,
                        source_column=src.name,
                        transformation=transformation,  # type: ignore[arg-type]
                        expression=None
                        if transformation == "direct"
                        else self._sql(inner),
                    )
                )

    # -- finalization ------------------------------------------------------------ #

    def finalize(self) -> None:
        """Resolve access types, flush tables/deps, emit SOURCE/SINK ops."""
        if self.streaming:
            for key, info in self._connectors.items():
                op_type = "SINK" if key in self._written_names else "SOURCE"
                self.add_operation(
                    op_type,
                    info.get("loc") or Location(),
                    {
                        "table": info["table"],
                        "connector": info.get("connector"),
                        "topic": info.get("topic"),
                    },
                )
        for key, ref in self._tables.items():
            is_read = key in self._read_names
            is_written = key in self._written_names
            if is_read and is_written:
                ref.access_type = "readwrite"
            elif is_written:
                ref.access_type = "write"
            else:
                ref.access_type = "read"
        self.ir.tables = list(self._tables.values())
        self.ir.dependencies = list(self._deps)
        if self._written_order and not self.ir.metadata.name:
            self.ir.metadata.name = self._written_order[0]


# --------------------------------------------------------------------------- #
# parse_sql
# --------------------------------------------------------------------------- #


def parse_sql(source: str, dialect: Optional[str]) -> ParseResult:
    """Parse warehouse SQL into the unified IR.

    Args:
        source: Raw SQL text (one or more statements).
        dialect: Optional dialect hint (``snowflake`` / ``bigquery`` /
            ``postgres`` / ``redshift`` / ``trino`` ...). Unknown dialects
            degrade to the generic sqlglot dialect with a warning.

    Raises:
        ParseError: when the SQL cannot be parsed (message + 1-based line).
    """
    if not source or not source.strip():
        raise ParseError("Empty SQL input - nothing to parse.")

    read, dialect_warning = _normalize_dialect(dialect)
    try:
        statements = sqlglot.parse(source, read=read)
    except SqlglotError as err:
        _raise_parse_error(err)
    except Exception as err:  # pragma: no cover - non-sqlglot failure
        raise ParseError(f"SQL parse error: {err}") from err

    statements = [s for s in statements if s is not None]
    if not statements:
        raise ParseError("No SQL statements found in input.")

    ir = IR(format="sql", dialect=dialect.strip().lower() if dialect else read)
    builder = _IRBuilder(ir, source, read)
    if dialect_warning:
        builder.warnings.append(dialect_warning)

    for idx, stmt in enumerate(statements, 1):
        try:
            builder.add_statement(stmt)
        except Exception:
            logger.exception("Failed to analyze SQL statement %d", idx)
            builder.warnings.append(
                f"Statement {idx} parsed but could not be fully analyzed."
            )
    builder.finalize()

    logger.debug(
        "parse_sql: %d statements, %d tables, %d operations",
        len(statements),
        len(ir.tables),
        len(ir.operations),
    )
    return ParseResult(
        ir=ir, source=source, ast=statements, extras={}, warnings=builder.warnings
    )


# --------------------------------------------------------------------------- #
# Flink helpers
# --------------------------------------------------------------------------- #


def _split_statements(source: str) -> List[Tuple[str, int]]:
    """Split SQL into ``(statement_text, start_line)`` pairs (1-based lines)."""
    boundaries: Optional[List[int]] = None
    try:
        tokens = sqlglot.tokenize(source, read="spark")
        boundaries = []
        for tok in tokens:
            if getattr(tok.token_type, "name", "") == "SEMICOLON":
                start = getattr(tok, "start", None)
                if not isinstance(start, int):
                    boundaries = None
                    break
                boundaries.append(start)
    except Exception:
        boundaries = None

    if boundaries is None:
        # Naive fallback: split on semicolons (best-effort; ';' inside string
        # literals would mis-split, which only degrades line attribution).
        boundaries = [i for i, ch in enumerate(source) if ch == ";"]

    spans: List[Tuple[int, int]] = []
    prev = 0
    for b in boundaries:
        spans.append((prev, b))
        prev = b + 1
    spans.append((prev, len(source)))

    out: List[Tuple[str, int]] = []
    for start, end in spans:
        chunk = source[start:end]
        if not chunk.strip():
            continue
        leading_ws = len(chunk) - len(chunk.lstrip())
        abs_start = start + leading_ws
        start_line = source[:abs_start].count("\n") + 1
        out.append((chunk.strip(), start_line))
    return out


def _blank_watermark_clauses(text: str) -> Tuple[str, bool]:
    """Blank ``WATERMARK FOR ... AS ...`` clauses, keeping line layout."""
    out = text
    changed = False
    rx = re.compile(r"WATERMARK\s+FOR", re.IGNORECASE)
    guard = 0
    while guard < 20:
        guard += 1
        m = rx.search(out)
        if m is None:
            break
        changed = True
        start = m.start()
        depth = 0
        i = m.end()
        end = len(out)
        ends_at_comma = False
        while i < len(out):
            ch = out[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                if depth == 0:
                    end = i
                    break
                depth -= 1
            elif ch == "," and depth == 0:
                end = i + 1  # consume the trailing comma too
                ends_at_comma = True
                break
            i += 1
        out = _blank_span(out, start, end)
        if not ends_at_comma:
            # Clause was the last schema item: blank the comma *before* it.
            j = start - 1
            while j >= 0 and out[j] in " \t\n":
                j -= 1
            if j >= 0 and out[j] == ",":
                out = out[:j] + " " + out[j + 1 :]
    return out, changed


def _blank_with_properties(text: str) -> Tuple[str, bool]:
    """Blank Flink connector ``WITH ('k' = 'v', ...)`` property blocks."""
    out = text
    changed = False
    rx = re.compile(r"\bWITH\s*\(", re.IGNORECASE)
    pos = 0
    guard = 0
    while guard < 20:
        guard += 1
        m = rx.search(out, pos)
        if m is None:
            break
        open_idx = out.index("(", m.start())
        close_idx = _matching_paren(out, open_idx)
        if close_idx is None:
            break
        region = out[open_idx : close_idx + 1]
        if "'" in region and "=" in region:
            out = _blank_span(out, m.start(), close_idx + 1)
            changed = True
            pos = m.start() + 1
        else:
            pos = close_idx + 1
    return out, changed


def _replace_window_tvfs(text: str) -> str:
    """Replace ``TABLE(TUMBLE(TABLE t, ...))`` TVFs with the inner table name."""
    out = text
    guard = 0
    while guard < 20:
        guard += 1
        m = _TVF_RX.search(out)
        if m is None:
            break
        open_idx = out.index("(", m.start())
        close_idx = _matching_paren(out, open_idx)
        if close_idx is None:
            break
        region = out[open_idx + 1 : close_idx]
        tm = re.search(r"\bTABLE\s+([`\"\w.]+)", region, re.IGNORECASE)
        if tm:
            name = _strip_quotes(tm.group(1))
        else:
            im = re.search(r"\(\s*([A-Za-z_][\w.]*)", region)
            name = im.group(1) if im else "stream_input"
        out = _blank_span(out, m.start(), close_idx + 1, replacement=name)
    return out


def _window_ops_from_text(text: str, offset: int) -> List[Operation]:
    """WINDOW ops for TUMBLE/HOP/SESSION/CUMULATE calls in a statement."""
    ops: List[Operation] = []
    seen: Set[Tuple[str, float, int]] = set()
    for m in _WINDOW_FN_RX.finditer(text):
        fn = m.group(1).lower()
        kind = _WINDOW_KIND_MAP.get(fn, fn)
        open_idx = text.index("(", m.start(1))
        close_idx = _matching_paren(text, open_idx)
        region = text[open_idx : (close_idx + 1) if close_idx is not None else len(text)]
        intervals = _INTERVAL_RX.findall(region)
        size_minutes = 0.0
        if intervals:
            # TVF forms list slide/step first and the window size last.
            value, unit = intervals[-1]
            size_minutes = _interval_minutes(value, unit)
        line = text[: m.start()].count("\n") + 1 + offset
        key = (kind, size_minutes, line)
        if key in seen:
            continue
        seen.add(key)
        ops.append(
            Operation(
                type="WINDOW",
                location=Location(line=line, col=m.start() - text.rfind("\n", 0, m.start())),
                details={"kind": kind, "size_minutes": size_minutes},
            )
        )
    return ops


def _watermark_ops_from_text(text: str, offset: int) -> List[Operation]:
    """WATERMARK ops for ``WATERMARK FOR col AS expr`` declarations."""
    ops: List[Operation] = []
    for i, line_text in enumerate(text.splitlines(), 1):
        m = _WATERMARK_RX.search(line_text)
        if m is None:
            continue
        column = _strip_quotes(m.group(1))
        expr = m.group(2).strip().rstrip(",")
        iv = _INTERVAL_RX.search(expr)
        delay = f"{iv.group(1)} {iv.group(2).upper()}" if iv else expr
        ops.append(
            Operation(
                type="WATERMARK",
                location=Location(line=i + offset, col=m.start() + 1),
                details={"column": column, "delay": delay},
            )
        )
    return ops


def _parse_flink_statement(
    text: str,
) -> Tuple[Optional[exp.Expression], bool]:
    """Parse one Flink statement with progressive tolerance.

    Returns ``(tree_or_None, stripped_flink_constructs)``.
    """
    prepared = _replace_window_tvfs(text)
    candidates: List[Tuple[str, bool]] = [(prepared, False)]
    no_wm, wm_changed = _blank_watermark_clauses(prepared)
    if wm_changed:
        candidates.append((no_wm, True))
    no_with, with_changed = _blank_with_properties(no_wm)
    if with_changed:
        candidates.append((no_with, True))
    for candidate, stripped in candidates:
        try:
            tree = sqlglot.parse_one(candidate, read="spark")
        except SqlglotError:
            continue
        except Exception:  # pragma: no cover - non-sqlglot failure
            logger.exception("Unexpected error parsing Flink statement")
            continue
        if tree is not None:
            return tree, stripped
    return None, False


def _fingerprint_create(builder: _IRBuilder, text: str, offset: int) -> None:
    """Register table/connector facts from an unparseable CREATE statement."""
    m = _CREATE_TABLE_RX.search(text)
    if m is None:
        return
    name = _strip_quotes(m.group(1))
    line = text[: m.start()].count("\n") + 1 + offset
    builder.register_raw_table(name)
    conn = _CONNECTOR_RX.search(text)
    if conn is not None:
        topic = _TOPIC_RX.search(text)
        builder.register_connector(
            name,
            conn.group(1),
            topic.group(1) if topic else None,
            Location(line=line, col=m.start() + 1),
        )


# --------------------------------------------------------------------------- #
# parse_flink
# --------------------------------------------------------------------------- #


def parse_flink(source: str) -> ParseResult:
    """Parse Flink streaming SQL into the unified IR.

    Flink-only DDL constructs (WATERMARK declarations, connector ``WITH``
    blocks, TUMBLE/HOP/SESSION/CUMULATE windowing TVFs) are stripped or
    rewritten so sqlglot can parse the rest; a warning is appended whenever a
    statement needed stripping. Emits the streaming operation vocabulary:
    SOURCE/SINK, WINDOW ``{kind, size_minutes}``, STATE ``{has_ttl}``,
    WATERMARK ``{column, delay}``, and CHECKPOINT only when checkpointing is
    configured.

    Raises:
        ParseError: when no statement could be parsed or fingerprinted.
    """
    if not source or not source.strip():
        raise ParseError("Empty Flink SQL input - nothing to parse.")

    ir = IR(format="flink", dialect="flink")
    ir.materialization.type = "stream"
    builder = _IRBuilder(ir, source, "spark", streaming=True)
    lower_source = source.lower()

    configs: Dict[str, str] = {}
    parsed: List[exp.Expression] = []

    for text, start_line in _split_statements(source):
        offset = start_line - 1
        if re.match(r"SET\b", text, re.IGNORECASE):
            for key, value in _SET_KV_RX.findall(text):
                configs[key.lower()] = value
            continue

        # Fingerprint streaming constructs from the raw statement text.
        ir.operations.extend(_watermark_ops_from_text(text, offset))
        window_ops = _window_ops_from_text(text, offset)
        ir.operations.extend(window_ops)

        if _CREATE_TABLE_RX.search(text) and _CONNECTOR_RX.search(text):
            _fingerprint_create(builder, text, offset)

        tree, stripped = _parse_flink_statement(text)
        if tree is None:
            builder.warnings.append(
                f"Could not fully parse Flink statement starting at line "
                f"{start_line}; recorded best-effort facts only."
            )
            continue
        if stripped:
            builder.warnings.append(
                f"Stripped WATERMARK/connector clauses to parse the statement "
                f"at line {start_line}."
            )
        parsed.append(tree)
        try:
            builder.add_statement(
                tree,
                line_offset=offset,
                stmt_text=text,
                stmt_has_window=bool(window_ops),
            )
        except Exception:
            logger.exception(
                "Failed to analyze Flink statement at line %d", start_line
            )
            builder.warnings.append(
                f"Flink statement at line {start_line} parsed but could not be "
                f"fully analyzed."
            )

    if not parsed and not builder._connectors and not builder._tables:
        raise ParseError(
            "Could not parse Flink SQL input - no recognizable statements found."
        )

    checkpoint_configured = any(
        "execution.checkpointing" in key for key in configs
    ) or "execution.checkpointing" in lower_source
    if checkpoint_configured:
        loc = Location()
        for i, line_text in enumerate(source.splitlines(), 1):
            if "execution.checkpointing" in line_text.lower():
                loc = Location(line=i, col=1)
                break
        ir.operations.append(
            Operation(
                type="CHECKPOINT",
                location=loc,
                details={
                    "configured": True,
                    "interval": configs.get("execution.checkpointing.interval"),
                },
            )
        )

    builder.finalize()

    extras: Dict[str, Any] = {
        "has_backpressure_config": any(
            token in lower_source for token in _BACKPRESSURE_TOKENS
        ),
        "configs": configs,
    }
    logger.debug(
        "parse_flink: %d statements, %d tables, %d operations",
        len(parsed),
        len(ir.tables),
        len(ir.operations),
    )
    return ParseResult(
        ir=ir, source=source, ast=parsed, extras=extras, warnings=builder.warnings
    )
