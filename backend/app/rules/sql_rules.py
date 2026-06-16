"""SQL anti-pattern rules.

Thirty-five deterministic checks over the sqlglot AST produced by the SQL /
dbt / Flink parsers. Every rule traverses ``pr.ast`` (a list of sqlglot
Expression statements) as its primary source and no-ops safely when the AST is
missing (tolerant parse failure). Line numbers are recovered by scanning
``pr.lines`` with rule-specific patterns, so they remain accurate even though
sqlglot does not retain token positions on expressions.

Precision over recall: each rule is deliberately conservative so a clean,
production-grade query produces zero findings.
"""
from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from typing import Dict, FrozenSet, Iterator, List, Optional, Sequence, Set, Tuple

from sqlglot import exp

from app.rules import Rule, register
from app.schemas.ir import ParseResult
from app.schemas.report import Issue

logger = logging.getLogger(__name__)

SQL_ONLY: FrozenSet[str] = frozenset({"sql"})
SQL_FLINK: FrozenSet[str] = frozenset({"sql", "flink"})
SQL_DBT: FrozenSet[str] = frozenset({"sql", "dbt"})
SQL_DBT_FLINK: FrozenSet[str] = frozenset({"sql", "dbt", "flink"})

#: Column names that typically drive partition pruning in cloud warehouses.
PARTITION_COLUMNS: FrozenSet[str] = frozenset(
    {
        "date",
        "ds",
        "dt",
        "event_date",
        "created_at",
        "_partitiondate",
        "_partitiontime",
        "_table_suffix",
        "partition_date",
        "event_dt",
    }
)

_DATE_LITERAL_RX = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?$")
_LARGE_TABLE_RX = re.compile(
    r"(^|_)(fact|events?|logs?|transactions?|history|clicks?|impressions?"
    r"|sessions?|orders?|sales?|pageviews?)($|_|s$)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# AST + source-line helpers
# ---------------------------------------------------------------------------

def _statements(pr: ParseResult) -> List[exp.Expression]:
    """Top-level sqlglot statements, or ``[]`` when no usable AST exists."""
    ast = pr.ast
    if not isinstance(ast, (list, tuple)):
        return []
    return [stmt for stmt in ast if isinstance(stmt, exp.Expression)]


class _LineIndex:
    """Best-effort mapper from AST findings to 1-based source line numbers.

    ``find`` keeps a per-pattern cursor so repeated occurrences of the same
    construct (e.g. several ``SELECT *``) resolve to successive lines.
    """

    def __init__(self, pr: ParseResult) -> None:
        self._lines: List[str] = pr.lines
        self._cursors: Dict[str, int] = {}

    def find(self, *patterns: str, advance: bool = True) -> Optional[int]:
        """First line matching any pattern (tried in order), 1-based."""
        for pattern in patterns:
            if not pattern:
                continue
            try:
                rx = re.compile(pattern, re.IGNORECASE)
            except re.error:
                continue
            start = self._cursors.get(pattern, 0) if advance else 0
            order = list(range(start, len(self._lines))) + list(range(0, start))
            for i in order:
                if rx.search(self._lines[i]):
                    if advance:
                        self._cursors[pattern] = i + 1
                    return i + 1
        return None

    def find_last(self, pattern: str) -> Optional[int]:
        """Last line matching the pattern (for trailing ORDER BY etc.)."""
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error:
            return None
        for i in range(len(self._lines) - 1, -1, -1):
            if rx.search(self._lines[i]):
                return i + 1
        return None

    def text(self, line_no: Optional[int]) -> str:
        """Raw text of a 1-based line, or empty string when out of range."""
        if line_no is None or line_no < 1 or line_no > len(self._lines):
            return ""
        return self._lines[line_no - 1]


def _diff(
    old: Sequence[str] | str,
    new: Sequence[str] | str,
    line_no: Optional[int] = None,
) -> str:
    """Build a unified diff string (``--- current`` / ``+++ optimized``)."""
    old_lines = [old] if isinstance(old, str) else list(old)
    new_lines = [new] if isinstance(new, str) else list(new)
    hunk = ""
    if line_no is not None:
        hunk = f"@@ -{line_no},{len(old_lines)} +{line_no},{len(new_lines)} @@\n"
    minus = "\n".join(f"-{line}" for line in old_lines)
    plus = "\n".join(f"+{line}" for line in new_lines)
    return f"--- current\n+++ optimized\n{hunk}{minus}\n{plus}"


def _replace_ci(text: str, needle: str, replacement: str) -> Optional[str]:
    """Case-insensitive single replacement; ``None`` when needle is absent."""
    idx = text.lower().find(needle.lower())
    if idx < 0:
        return None
    return text[:idx] + replacement + text[idx + len(needle):]


def _truncate(sql: str, limit: int = 90) -> str:
    """Collapse whitespace and clip long SQL snippets for messages."""
    flat = " ".join(sql.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _indent(line: str) -> str:
    """Leading whitespace of a source line (to keep diffs aligned)."""
    return line[: len(line) - len(line.lstrip())]


def _tname(table: exp.Table) -> str:
    """Fully qualified display name of a table reference."""
    parts = [p for p in (table.catalog, table.db, table.name) if p]
    return ".".join(parts) if parts else _truncate(table.sql(), 40)


def _alias_or_name(table: exp.Table) -> str:
    """The identifier other clauses use to refer to this table."""
    return table.alias or table.name


def _from_clause(select: exp.Expression) -> Optional[exp.From]:
    """The FROM clause of a select, tolerant of sqlglot arg-key renames.

    Newer sqlglot releases store the clause under ``from_`` (the older key was
    ``from``); checking both keeps the rules working across versions.
    """
    from_ = select.args.get("from_") or select.args.get("from")
    return from_ if isinstance(from_, exp.From) else None


def _cte_names(stmt: exp.Expression) -> Set[str]:
    """Lower-cased CTE names defined anywhere in the statement."""
    return {
        cte.alias_or_name.lower()
        for cte in stmt.find_all(exp.CTE)
        if cte.alias_or_name
    }


def _is_within(node: exp.Expression, ancestor: exp.Expression) -> bool:
    """True when ``ancestor`` is on the parent chain of ``node``."""
    parent = node.parent
    while parent is not None:
        if parent is ancestor:
            return True
        parent = parent.parent
    return False


def _direct_sources(select: exp.Select) -> List[Tuple[str, exp.Expression]]:
    """(lowercased alias-or-name, source node) for FROM plus each JOIN."""
    sources: List[exp.Expression] = []
    from_ = _from_clause(select)
    if from_ is not None:
        sources.append(from_.this)
    for join in select.args.get("joins") or []:
        if isinstance(join, exp.Join):
            sources.append(join.this)
    named: List[Tuple[str, exp.Expression]] = []
    for src in sources:
        if isinstance(src, exp.Table) and src.name:
            named.append((_alias_or_name(src).lower(), src))
        elif isinstance(src, exp.Subquery) and src.alias:
            named.append((src.alias.lower(), src))
    return named


def _direct_tables(select: exp.Select) -> List[exp.Table]:
    """Physical table nodes referenced directly by FROM / JOIN."""
    return [src for _, src in _direct_sources(select) if isinstance(src, exp.Table)]


def _read_tables(stmt: exp.Expression) -> List[exp.Table]:
    """Tables the statement *reads* (FROM/JOIN), excluding CTE references."""
    ctes = _cte_names(stmt)
    out: List[exp.Table] = []
    for table in stmt.find_all(exp.Table):
        if not table.name or table.name.lower() in ctes:
            continue
        anchor = table.find_ancestor(exp.From, exp.Join, exp.Insert, exp.Update)
        if isinstance(anchor, (exp.From, exp.Join)):
            out.append(table)
    return out


def _has_aggregation(select: exp.Select) -> bool:
    """True when the select dedups or aggregates (GROUP BY / DISTINCT / aggs)."""
    if select.args.get("group") or select.args.get("distinct"):
        return True
    return any(
        proj.find(exp.AggFunc) is not None for proj in select.expressions
    )


def _in_exists(node: exp.Expression) -> bool:
    """True when the node lives inside an EXISTS predicate."""
    return node.find_ancestor(exp.Exists) is not None


def _clause_top(node: exp.Expression, ancestor: exp.Expression) -> Optional[str]:
    """arg_key of the child-of-``ancestor`` that contains ``node``."""
    current = node
    while current.parent is not None and current.parent is not ancestor:
        current = current.parent
    return current.arg_key if current.parent is ancestor else None


def _recent_date_predicate(dialect: str, column: str) -> str:
    """Dialect-appropriate "last 7 days" predicate used in suggested diffs."""
    if dialect == "bigquery":
        return f"{column} >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)"
    if dialect == "snowflake":
        return f"{column} >= DATEADD('day', -7, CURRENT_DATE)"
    return f"{column} >= CURRENT_DATE - INTERVAL '7 day'"


def _partition_column_hint(pr: ParseResult, dialect: str) -> str:
    """Best partition column to suggest, from IR metadata or convention."""
    partition_by = pr.ir.materialization.partition_by
    if partition_by:
        return partition_by[0]
    return "_PARTITIONDATE" if dialect == "bigquery" else "event_date"


def _normalized_join_condition(target: exp.Table, on: exp.Expression) -> str:
    """Join condition SQL with the join side's alias folded into the table name.

    Makes ``JOIN customers c1 ON c1.id = o.cid`` and ``JOIN customers c2 ON
    c2.id = o.cid`` comparable, so copy-pasted joins are recognized.
    """
    sql = on.sql().lower()
    alias = (target.alias or "").lower()
    if alias and alias != target.name.lower():
        sql = re.sub(rf"\b{re.escape(alias)}\s*\.", f"{target.name.lower()}.", sql)
    return re.sub(r"\s+", "", sql)


def _where_equi_join_predicate(
    select: exp.Select, left_names: Set[str], right_names: Set[str]
) -> Optional[str]:
    """An implicit join predicate hiding in WHERE (classic comma-join fix)."""
    where = select.args.get("where")
    if not isinstance(where, exp.Where):
        return None
    for eq in where.find_all(exp.EQ):
        lhs, rhs = eq.this, eq.expression
        if isinstance(lhs, exp.Column) and isinstance(rhs, exp.Column):
            lt, rt = lhs.table.lower(), rhs.table.lower()
            if not lt or not rt:
                continue
            if (lt in left_names and rt in right_names) or (
                lt in right_names and rt in left_names
            ):
                return eq.sql()
    return None


# ---------------------------------------------------------------------------
# CRITICAL rules
# ---------------------------------------------------------------------------

@register
class SelectStarRule(Rule):
    """``SELECT *`` in a production query scans and ships every column."""

    id = "SELECT_STAR"
    severity = "CRITICAL"
    category = "performance"
    formats = SQL_DBT_FLINK
    title = "SELECT * in production query"
    description = (
        "SELECT * reads every column, defeating columnar pruning, inflating "
        "bytes scanned, and silently breaking downstream consumers when the "
        "upstream schema changes."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag bare ``*`` / ``t.*`` projections outside EXISTS predicates."""
        issues: List[Issue] = []
        idx = _LineIndex(pr)
        for stmt in _statements(pr):
            for select in stmt.find_all(exp.Select):
                if _in_exists(select):
                    continue
                for proj in select.expressions:
                    star: Optional[exp.Expression] = None
                    qualifier = ""
                    if isinstance(proj, exp.Star):
                        star = proj
                    elif isinstance(proj, exp.Column) and isinstance(proj.this, exp.Star):
                        star = proj.this
                        qualifier = proj.table
                    if star is None:
                        continue
                    if star.args.get("except") or star.args.get("replace"):
                        continue  # BigQuery * EXCEPT(...) is column-managed.
                    tables = [_tname(t) for t in _direct_tables(select)] or ["the source"]
                    subject = f"{qualifier}.*" if qualifier else "*"
                    line = idx.find(
                        rf"select\s+(distinct\s+)?{re.escape(subject)}",
                        r"select\s+(distinct\s+)?\*",
                        r"\bselect\b",
                    )
                    issues.append(
                        self.issue(
                            f"SELECT {subject} reads every column of "
                            f"{', '.join(tables)}; columnar engines bill for all "
                            "of them and schema drift propagates silently.",
                            line=line,
                            fix_suggestion=(
                                "Enumerate only the columns this query actually "
                                "uses instead of " + subject + "."
                            ),
                            fix_diff=self._build_diff(idx, line, subject, stmt, select),
                        )
                    )
        return issues

    def _build_diff(
        self,
        idx: _LineIndex,
        line: Optional[int],
        subject: str,
        stmt: exp.Expression,
        select: exp.Select,
    ) -> Optional[str]:
        """Replace the star with columns the statement provably references."""
        text = idx.text(line)
        if not text or "*" not in text:
            return None
        referenced: List[str] = []
        seen: Set[str] = set()
        for clause in ("joins", "where", "group", "having", "order", "qualify"):
            node = select.args.get(clause)
            nodes = node if isinstance(node, list) else [node]
            for sub in nodes:
                if not isinstance(sub, exp.Expression):
                    continue
                for col in sub.find_all(exp.Column):
                    sql = col.sql()
                    if sql.lower() not in seen:
                        seen.add(sql.lower())
                        referenced.append(sql)
        if not referenced:
            return None
        column_list = ", ".join(referenced[:6])
        new_text = text.replace(subject, column_list, 1)
        if new_text == text:
            new_text = text.replace("*", column_list, 1)
        return _diff(text, new_text, line)


@register
class CartesianJoinRule(Rule):
    """CROSS / comma / ON-less joins multiply row counts."""

    id = "CARTESIAN_JOIN"
    severity = "CRITICAL"
    category = "performance"
    formats = SQL_DBT_FLINK
    title = "Cartesian join"
    description = (
        "A CROSS JOIN, comma-join, or JOIN without ON/USING produces the "
        "cartesian product of both sides - row counts multiply and the query "
        "cost grows quadratically."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag joins with no join condition, excluding UNNEST/LATERAL."""
        issues: List[Issue] = []
        idx = _LineIndex(pr)
        for stmt in _statements(pr):
            for select in stmt.find_all(exp.Select):
                from_ = _from_clause(select)
                left = from_.this if from_ is not None else None
                left_disp = (
                    _alias_or_name(left) if isinstance(left, exp.Table) else "left side"
                )
                for join in select.args.get("joins") or []:
                    if not isinstance(join, exp.Join):
                        continue
                    if (join.args.get("method") or "").upper() in {"NATURAL", "ASOF"}:
                        continue
                    target = join.this
                    if isinstance(target, (exp.Unnest, exp.Lateral, exp.Values)):
                        continue
                    has_condition = bool(join.args.get("on") or join.args.get("using"))
                    is_cross = join.kind == "CROSS"
                    if has_condition or (not is_cross and join.kind == "SEMI"):
                        continue
                    if not is_cross and not has_condition:
                        pass  # comma-join or JOIN missing ON
                    right_disp = (
                        _alias_or_name(target)
                        if isinstance(target, exp.Table)
                        else _truncate(target.sql(), 40)
                    )
                    kind_label = "CROSS JOIN" if is_cross else "join without ON/USING"
                    line = idx.find(
                        rf"cross\s+join\s+\S*{re.escape(right_disp)}" if is_cross else "",
                        r"\bcross\s+join\b" if is_cross else "",
                        rf"\bjoin\s+\S*{re.escape(right_disp)}",
                        rf",\s*{re.escape(right_disp)}\b",
                        r"\bfrom\b",
                    )
                    pred = _where_equi_join_predicate(
                        select, {left_disp.lower()}, {right_disp.lower()}
                    ) or f"{left_disp}.<join_key> = {right_disp}.<join_key>"
                    issues.append(
                        self.issue(
                            f"{kind_label} between {left_disp} and {right_disp} "
                            "produces a cartesian product - every row on one side "
                            "pairs with every row on the other.",
                            line=line,
                            fix_suggestion=(
                                f"Join {right_disp} with an explicit equi-condition, "
                                f"e.g. ON {pred}."
                            ),
                            fix_diff=self._build_diff(idx, line, is_cross, right_disp, pred),
                        )
                    )
        return issues

    def _build_diff(
        self,
        idx: _LineIndex,
        line: Optional[int],
        is_cross: bool,
        right_disp: str,
        pred: str,
    ) -> Optional[str]:
        """Rewrite the offending line into an explicit equi-join."""
        text = idx.text(line)
        if not text:
            return None
        pad = _indent(text)
        if is_cross:
            replaced = _replace_ci(text, "cross join", "INNER JOIN")
            if replaced is None:
                return None
            return _diff(text, [replaced, f"{pad}  ON {pred}"], line)
        stripped = re.sub(
            rf"(?i),\s*(\w+\.)*{re.escape(right_disp)}\b(\s+as)?(\s+\w+)?",
            "",
            text,
            count=1,
        )
        if stripped != text:
            return _diff(text, [stripped, f"{pad}JOIN {right_disp} ON {pred}"], line)
        replaced = _replace_ci(text, "join", "INNER JOIN")
        if replaced is None:
            return None
        return _diff(text, [replaced, f"{pad}  ON {pred}"], line)


@register
class UnboundedFullScanRule(Rule):
    """A statement reading physical tables without any WHERE clause."""

    id = "UNBOUNDED_FULL_SCAN"
    severity = "CRITICAL"
    category = "performance"
    formats = SQL_DBT
    title = "Unbounded full table scan"
    description = (
        "The statement reads physical tables with no WHERE clause anywhere, so "
        "the warehouse must scan every row on every run."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag SELECT/INSERT/CTAS statements with table reads and no WHERE."""
        issues: List[Issue] = []
        idx = _LineIndex(pr)
        dialect = (pr.ir.dialect or "").lower()
        if pr.ir.materialization.type == "view":
            return issues
        for stmt in _statements(pr):
            if isinstance(stmt, exp.Create):
                if (stmt.kind or "").upper() != "TABLE":
                    continue
            elif not isinstance(stmt, (exp.Select, exp.Union, exp.Insert)):
                continue
            if stmt.find(exp.Where) is not None:
                continue
            if stmt.find(exp.Limit, exp.Fetch) is not None:
                continue
            reads = _read_tables(stmt)
            if not reads:
                continue
            names = sorted({_tname(t) for t in reads})
            first = reads[0]
            line = idx.find(
                rf"\bfrom\s+\S*{re.escape(first.name)}\b", r"\bfrom\b"
            )
            column = _partition_column_hint(pr, dialect)
            predicate = _recent_date_predicate(dialect, column)
            text = idx.text(line)
            fix_diff = (
                _diff(text, [text, f"{_indent(text)}WHERE {predicate}"], line)
                if text
                else None
            )
            issues.append(
                self.issue(
                    f"Statement scans {', '.join(names)} with no WHERE clause at "
                    "all - every run reads the entire table(s).",
                    line=line,
                    fix_suggestion=(
                        "Add a WHERE predicate (ideally on a partition/date "
                        f"column such as {column}) to bound the scan."
                    ),
                    fix_diff=fix_diff,
                )
            )
        return issues


@register
class ImplicitCastInJoinRule(Rule):
    """Casts or functions on join keys disable index/pruning-based joins."""

    id = "IMPLICIT_CAST_IN_JOIN"
    severity = "CRITICAL"
    category = "performance"
    formats = SQL_DBT_FLINK
    title = "Implicit cast in join condition"
    description = (
        "Wrapping a join key in CAST or a function (or comparing mismatched "
        "literal types) forces a type coercion per row, blocks join pruning, "
        "and frequently degenerates into a nested-loop join."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag CAST/function-wrapped join keys and string-vs-number compares."""
        issues: List[Issue] = []
        idx = _LineIndex(pr)
        for stmt in _statements(pr):
            for join in stmt.find_all(exp.Join):
                on = join.args.get("on")
                if not isinstance(on, exp.Expression):
                    continue
                for eq in on.find_all(exp.EQ):
                    issue = self._check_eq(eq, idx)
                    if issue is not None:
                        issues.append(issue)
        return issues

    def _check_eq(self, eq: exp.EQ, idx: _LineIndex) -> Optional[Issue]:
        """Inspect one equality inside a join condition."""
        for side, other in ((eq.this, eq.expression), (eq.expression, eq.this)):
            if isinstance(side, exp.Cast):
                col = side.find(exp.Column)
                if col is None:
                    continue
                col_sql = col.sql()
                line = idx.find(
                    rf"cast\s*\(\s*{re.escape(col_sql)}",
                    rf"{re.escape(col_sql)}\s*::",
                    rf"\b{re.escape(col.name)}\b",
                )
                return self.issue(
                    f"Join condition casts {col_sql} to "
                    f"{_truncate(side.args.get('to').sql() if side.args.get('to') else 'another type', 30)} "
                    f"before comparing with {_truncate(other.sql(), 40)} - the cast "
                    "runs per row and disables join pruning.",
                    line=line,
                    fix_suggestion=(
                        f"Align the column types upstream so {col_sql} joins "
                        "without a cast."
                    ),
                    fix_diff=self._cast_diff(idx, line, col_sql),
                )
            if isinstance(side, exp.Func) and not isinstance(side, exp.Cast):
                col = side.find(exp.Column)
                if col is None:
                    continue
                if isinstance(side, exp.Anonymous) and side.this:
                    func_name = str(side.this).upper()
                else:
                    func_name = (
                        side.sql_name()
                        if hasattr(side, "sql_name")
                        else type(side).__name__
                    )
                line = idx.find(
                    rf"{re.escape(func_name)}\s*\(.*{re.escape(col.name)}",
                    rf"\b{re.escape(col.name)}\b",
                )
                return self.issue(
                    f"Join condition wraps {col.sql()} in {func_name}() - the "
                    "function is evaluated for every candidate row pair.",
                    line=line,
                    fix_suggestion=(
                        f"Precompute {func_name}({col.sql()}) into a column "
                        "upstream and join on the raw value."
                    ),
                )
            if (
                isinstance(side, exp.Column)
                and isinstance(other, exp.Literal)
                and other.is_string
                and other.this.isdigit()
            ):
                line = idx.find(rf"{re.escape(side.name)}\s*=\s*'{re.escape(other.this)}'")
                return self.issue(
                    f"Join compares column {side.sql()} to string literal "
                    f"'{other.this}' - a numeric column would be implicitly cast "
                    "on every row.",
                    line=line,
                    fix_suggestion=(
                        f"Use a literal of the column's native type: "
                        f"{side.sql()} = {other.this}."
                    ),
                )
        return None

    def _cast_diff(
        self, idx: _LineIndex, line: Optional[int], col_sql: str
    ) -> Optional[str]:
        """Strip the cast from the join key on the offending line."""
        text = idx.text(line)
        if not text:
            return None
        new_text = re.sub(r"(?i)\b(try_)?cast\s*\([^()]*\)", col_sql, text, count=1)
        if new_text == text:
            new_text = re.sub(
                rf"(?i){re.escape(col_sql)}\s*::\s*\w+", col_sql, text, count=1
            )
        if new_text == text:
            return None
        return _diff(text, new_text + "  -- align types upstream", line)


@register
class NestedLoopRiskRule(Rule):
    """Scalar subqueries in the projection run once per output row."""

    id = "NESTED_LOOP_RISK"
    severity = "CRITICAL"
    category = "performance"
    formats = SQL_DBT
    title = "Scalar subquery in SELECT list"
    description = (
        "A subquery inside the SELECT projection executes once per outer row - "
        "an O(n*m) nested loop that should be a single join or window."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag subqueries embedded in projection expressions."""
        issues: List[Issue] = []
        idx = _LineIndex(pr)
        for stmt in _statements(pr):
            for select in stmt.find_all(exp.Select):
                for proj in select.expressions:
                    for sub in proj.find_all(exp.Subquery):
                        if sub.find_ancestor(exp.In, exp.Exists) is not None:
                            continue
                        inner_tables = [
                            _tname(t)
                            for t in sub.find_all(exp.Table)
                        ] or ["a subquery"]
                        alias = proj.alias if isinstance(proj, exp.Alias) else ""
                        label = f" (column {alias})" if alias else ""
                        line = idx.find(r"\(\s*select\b", r"\bselect\b")
                        issues.append(
                            self.issue(
                                f"Scalar subquery over {inner_tables[0]} in the "
                                f"SELECT list{label} executes once per outer row - "
                                "a nested loop over the full result set.",
                                line=line,
                                fix_suggestion=(
                                    f"Pre-aggregate {inner_tables[0]} with GROUP BY "
                                    "and LEFT JOIN it once, or use a window function."
                                ),
                            )
                        )
        return issues


@register
class DeleteWithoutWhereRule(Rule):
    """DELETE with no WHERE removes every row in the table."""

    id = "DELETE_WITHOUT_WHERE"
    severity = "CRITICAL"
    category = "reliability"
    formats = SQL_ONLY
    title = "DELETE without WHERE"
    description = (
        "A DELETE statement with no WHERE clause irreversibly removes every "
        "row of the target table."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag every DELETE lacking a WHERE clause."""
        issues: List[Issue] = []
        idx = _LineIndex(pr)
        for stmt in _statements(pr):
            for delete in stmt.find_all(exp.Delete):
                if delete.args.get("where") is not None:
                    continue
                target = delete.this
                name = _tname(target) if isinstance(target, exp.Table) else "the table"
                line = idx.find(
                    rf"delete\s+from\s+\S*{re.escape(name.split('.')[-1])}",
                    r"\bdelete\s+from\b",
                    r"\bdelete\b",
                )
                text = idx.text(line)
                fix_diff = (
                    _diff(
                        text,
                        [
                            text,
                            f"{_indent(text)}WHERE <row_filter>  "
                            f"-- or use TRUNCATE TABLE {name} if a full purge is intended",
                        ],
                        line,
                    )
                    if text
                    else None
                )
                issues.append(
                    self.issue(
                        f"DELETE FROM {name} has no WHERE clause - every row in "
                        f"{name} will be removed on each run.",
                        line=line,
                        fix_suggestion=(
                            "Add an explicit WHERE predicate, or use TRUNCATE "
                            "TABLE if wiping the table is intentional."
                        ),
                        fix_diff=fix_diff,
                    )
                )
        return issues


@register
class UpdateWithoutWhereRule(Rule):
    """UPDATE with no WHERE rewrites every row in the table."""

    id = "UPDATE_WITHOUT_WHERE"
    severity = "CRITICAL"
    category = "reliability"
    formats = SQL_ONLY
    title = "UPDATE without WHERE"
    description = (
        "An UPDATE statement with no WHERE clause rewrites every row of the "
        "target table - almost always a destructive mistake."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag every UPDATE lacking a WHERE clause."""
        issues: List[Issue] = []
        idx = _LineIndex(pr)
        for stmt in _statements(pr):
            for update in stmt.find_all(exp.Update):
                if update.args.get("where") is not None:
                    continue
                target = update.this
                name = _tname(target) if isinstance(target, exp.Table) else "the table"
                line = idx.find(
                    rf"update\s+\S*{re.escape(name.split('.')[-1])}",
                    r"\bupdate\b",
                )
                text = idx.text(line)
                set_line = idx.find(r"\bset\b") or line
                set_text = idx.text(set_line)
                anchor = set_text or text
                anchor_line = set_line if set_text else line
                fix_diff = (
                    _diff(
                        anchor,
                        [anchor, f"{_indent(anchor)}WHERE <row_filter>  -- scope the update"],
                        anchor_line,
                    )
                    if anchor
                    else None
                )
                issues.append(
                    self.issue(
                        f"UPDATE {name} has no WHERE clause - every row in "
                        f"{name} will be rewritten on each run.",
                        line=line,
                        fix_suggestion="Add a WHERE predicate restricting which rows are updated.",
                        fix_diff=fix_diff,
                    )
                )
        return issues


# ---------------------------------------------------------------------------
# WARNING rules
# ---------------------------------------------------------------------------

@register
class CorrelatedSubqueryRule(Rule):
    """Correlated subqueries in WHERE/HAVING re-execute per outer row."""

    id = "CORRELATED_SUBQUERY"
    severity = "WARNING"
    category = "performance"
    formats = SQL_DBT
    title = "Correlated subquery"
    description = (
        "A subquery that references outer-query columns is re-evaluated for "
        "each outer row unless the optimizer can decorrelate it."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag WHERE/HAVING subqueries referencing outer table aliases."""
        issues: List[Issue] = []
        idx = _LineIndex(pr)
        for stmt in _statements(pr):
            for sub in stmt.find_all(exp.Subquery):
                outer = sub.find_ancestor(exp.Select)
                if outer is None:
                    continue
                clause = _clause_top(sub, outer)
                if clause not in {"where", "having"}:
                    continue
                if _in_exists(sub):
                    continue  # EXISTS is usually planned as a semi-join.
                refs = self._correlated_refs(sub, outer)
                if not refs:
                    continue
                inner_tables = [
                    _tname(t) for t in sub.find_all(exp.Table)
                ] or ["the subquery"]
                line = idx.find(r"\(\s*select\b", r"\bselect\b")
                issues.append(
                    self.issue(
                        f"Subquery over {inner_tables[0]} is correlated with the "
                        f"outer query via {', '.join(sorted(refs))} - it may "
                        "execute once per outer row.",
                        line=line,
                        fix_suggestion=(
                            "Rewrite as a JOIN against a pre-aggregated derived "
                            "table, or use a window function."
                        ),
                    )
                )
        return issues

    @staticmethod
    def _correlated_refs(sub: exp.Subquery, outer: exp.Select) -> Set[str]:
        """Outer-scope column references used inside the subquery."""
        inner_names: Set[str] = set()
        for table in sub.find_all(exp.Table):
            inner_names.add(table.name.lower())
            if table.alias:
                inner_names.add(table.alias.lower())
        for nested in sub.find_all(exp.Subquery):
            if nested is not sub and nested.alias:
                inner_names.add(nested.alias.lower())
        for cte in sub.find_all(exp.CTE):
            inner_names.add(cte.alias_or_name.lower())
        outer_names: Set[str] = set()
        node: Optional[exp.Expression] = sub.parent
        while node is not None:
            if isinstance(node, exp.Select):
                outer_names |= {alias for alias, _ in _direct_sources(node)}
            node = node.parent
        refs: Set[str] = set()
        for col in sub.find_all(exp.Column):
            qualifier = col.table.lower()
            if qualifier and qualifier not in inner_names and qualifier in outer_names:
                refs.add(col.sql())
        return refs


@register
class OrInWhereRule(Rule):
    """OR chains in WHERE defeat index usage and partition pruning."""

    id = "OR_IN_WHERE_DEFEATS_INDEX"
    severity = "WARNING"
    category = "performance"
    formats = SQL_DBT_FLINK
    title = "OR in WHERE defeats index"
    description = (
        "OR-connected predicates prevent most engines from using a single "
        "index or pruning path; IN lists or UNION ALL branches plan better."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag the top OR chain of each WHERE clause."""
        issues: List[Issue] = []
        idx = _LineIndex(pr)
        for stmt in _statements(pr):
            for where in stmt.find_all(exp.Where):
                top_or = where.find(exp.Or)
                if top_or is None:
                    continue
                operands = list(top_or.flatten())
                same_col, literals = self._same_column_eq_chain(operands)
                line = idx.find(r"\bor\b")
                if same_col:
                    in_list = ", ".join(literals)
                    issues.append(
                        self.issue(
                            f"WHERE chains {len(operands)} OR-equalities on "
                            f"{same_col} - engines plan IN lists far better.",
                            line=line,
                            fix_suggestion=f"Replace with {same_col} IN ({in_list}).",
                            fix_diff=self._in_list_diff(idx, line, same_col, in_list),
                        )
                    )
                else:
                    cols = sorted(
                        {
                            col.sql()
                            for operand in operands
                            for col in operand.find_all(exp.Column)
                        }
                    )[:4]
                    issues.append(
                        self.issue(
                            "WHERE combines predicates on "
                            f"{', '.join(cols) if cols else 'multiple expressions'} "
                            "with OR - the engine cannot use a single index or "
                            "pruning path.",
                            line=line,
                            fix_suggestion=(
                                "Split into UNION ALL branches with one sargable "
                                "predicate each, or restructure into an IN list."
                            ),
                        )
                    )
        return issues

    @staticmethod
    def _same_column_eq_chain(
        operands: List[exp.Expression],
    ) -> Tuple[Optional[str], List[str]]:
        """Detect ``col = a OR col = b OR ...`` chains."""
        column: Optional[str] = None
        literals: List[str] = []
        for operand in operands:
            if not isinstance(operand, exp.EQ):
                return None, []
            lhs, rhs = operand.this, operand.expression
            if isinstance(lhs, exp.Column) and isinstance(rhs, exp.Literal):
                col_sql, lit = lhs.sql(), rhs.sql()
            elif isinstance(rhs, exp.Column) and isinstance(lhs, exp.Literal):
                col_sql, lit = rhs.sql(), lhs.sql()
            else:
                return None, []
            if column is None:
                column = col_sql
            elif column.lower() != col_sql.lower():
                return None, []
            literals.append(lit)
        return (column, literals) if column and len(literals) >= 2 else (None, [])

    @staticmethod
    def _in_list_diff(
        idx: _LineIndex, line: Optional[int], column: str, in_list: str
    ) -> Optional[str]:
        """Rewrite ``col = a OR col = b`` into ``col IN (a, b)`` on one line."""
        text = idx.text(line)
        if not text:
            return None
        col_rx = re.escape(column)
        pattern = rf"(?i){col_rx}\s*=\s*[^\s()]+(\s+or\s+{col_rx}\s*=\s*[^\s()]+)+"
        new_text, count = re.subn(pattern, f"{column} IN ({in_list})", text, count=1)
        if count == 0:
            return None
        return _diff(text, new_text, line)


@register
class LeadingWildcardLikeRule(Rule):
    """LIKE patterns starting with a wildcard cannot use indexes."""

    id = "LEADING_WILDCARD_LIKE"
    severity = "WARNING"
    category = "performance"
    formats = SQL_DBT_FLINK
    title = "Leading wildcard LIKE"
    description = (
        "LIKE/ILIKE patterns beginning with % or _ cannot be served by an "
        "index or zone map - the engine scans and pattern-matches every row."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag LIKE/ILIKE whose pattern literal starts with % or _."""
        issues: List[Issue] = []
        idx = _LineIndex(pr)
        for stmt in _statements(pr):
            for like in stmt.find_all(exp.Like, exp.ILike):
                pattern = like.expression
                if not (isinstance(pattern, exp.Literal) and pattern.is_string):
                    continue
                value = pattern.this or ""
                if not value or value[0] not in "%_":
                    continue
                column = _truncate(like.this.sql(), 40)
                op = "ILIKE" if isinstance(like, exp.ILike) else "LIKE"
                line = idx.find(
                    rf"i?like\s+'{re.escape(value)}'", r"\bi?like\b"
                )
                issues.append(
                    self.issue(
                        f"{column} {op} '{value}' starts with a wildcard - no "
                        "index or pruning can help, forcing a full scan plus "
                        "per-row pattern matching.",
                        line=line,
                        fix_suggestion=(
                            "Anchor the pattern (LIKE 'term%'), or move this "
                            "lookup to a full-text/search-optimized index."
                        ),
                    )
                )
        return issues


@register
class UnoptimizedCteReuseRule(Rule):
    """CTEs referenced many times may be re-executed per reference."""

    id = "UNOPTIMIZED_CTE_REUSE"
    severity = "WARNING"
    category = "performance"
    formats = SQL_DBT
    title = "CTE referenced 3+ times"
    description = (
        "Several engines (and all engines under certain plans) inline CTEs, "
        "re-executing the CTE body once per reference."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag CTEs referenced three or more times in one statement."""
        issues: List[Issue] = []
        idx = _LineIndex(pr)
        for stmt in _statements(pr):
            for cte in stmt.find_all(exp.CTE):
                name = cte.alias_or_name
                if not name:
                    continue
                refs = sum(
                    1
                    for table in stmt.find_all(exp.Table)
                    if table.name.lower() == name.lower() and not _is_within(table, cte)
                )
                if refs < 3:
                    continue
                line = idx.find(
                    rf"\b{re.escape(name)}\s+as\s*\(", rf"\b{re.escape(name)}\b"
                )
                issues.append(
                    self.issue(
                        f"CTE {name} is referenced {refs} times - engines that "
                        "inline CTEs will execute its body "
                        f"{refs}x.",
                        line=line,
                        fix_suggestion=(
                            f"Materialize {name} as a temporary table (or its own "
                            "dbt model) so it is computed once."
                        ),
                    )
                )
        return issues


@register
class MissingPartitionFilterRule(Rule):
    """Warehouse queries without a partition predicate scan all partitions."""

    id = "MISSING_PARTITION_FILTER"
    severity = "WARNING"
    category = "cost"
    formats = SQL_DBT
    title = "Missing partition filter"
    description = (
        "On Snowflake/BigQuery, filtering on the partition column is the "
        "single biggest lever on bytes scanned; without it every partition is "
        "read and billed."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag warehouse statements whose WHERE never touches a partition column."""
        issues: List[Issue] = []
        dialect = (pr.ir.dialect or "").lower()
        if dialect not in {"snowflake", "bigquery"}:
            return issues
        partition_names = set(PARTITION_COLUMNS) | {
            c.lower() for c in pr.ir.materialization.partition_by
        }
        idx = _LineIndex(pr)
        for stmt in _statements(pr):
            wheres = list(stmt.find_all(exp.Where))
            if not wheres:
                continue  # No WHERE at all is UNBOUNDED_FULL_SCAN's territory.
            reads = _read_tables(stmt)
            if not reads:
                continue
            filtered = {
                col.name.lower()
                for where in wheres
                for col in where.find_all(exp.Column)
            }
            if filtered & partition_names:
                continue
            names = sorted({_tname(t) for t in reads})
            line = idx.find(r"\bwhere\b")
            column = _partition_column_hint(pr, dialect)
            predicate = _recent_date_predicate(dialect, column)
            text = idx.text(line)
            fix_diff = (
                _diff(text, [text, f"{_indent(text)}  AND {predicate}"], line)
                if text
                else None
            )
            issues.append(
                self.issue(
                    f"{dialect} query filters on "
                    f"{', '.join(sorted(filtered)) if filtered else 'no columns'} "
                    f"but never on a partition column while reading "
                    f"{', '.join(names)} - all partitions are scanned and billed.",
                    line=line,
                    fix_suggestion=(
                        f"Add a predicate on the partition column (e.g. {column}) "
                        "so the warehouse can prune partitions."
                    ),
                    fix_diff=fix_diff,
                )
            )
        return issues


@register
class ExplodingJoinRule(Rule):
    """Non-equi joins and repeated join sides can fan out row counts."""

    id = "EXPLODING_JOIN"
    severity = "WARNING"
    category = "performance"
    formats = SQL_DBT_FLINK
    title = "Exploding join"
    description = (
        "A join on a non-equi condition (ranges, inequality, constants) or the "
        "same table joined twice without aggregation can multiply rows instead "
        "of matching them 1:1."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag non-equi join conditions and duplicate join sides."""
        issues: List[Issue] = []
        idx = _LineIndex(pr)
        for stmt in _statements(pr):
            for select in stmt.find_all(exp.Select):
                issues.extend(self._non_equi(select, idx))
                issues.extend(self._double_join(select, idx))
        return issues

    def _non_equi(self, select: exp.Select, idx: _LineIndex) -> List[Issue]:
        """Joins whose ON contains no equality at all."""
        out: List[Issue] = []
        for join in select.args.get("joins") or []:
            on = join.args.get("on")
            if not isinstance(on, exp.Expression):
                continue
            if on.find(exp.EQ) is not None or on.find(exp.NullSafeEQ) is not None:
                continue
            target = join.this
            name = _tname(target) if isinstance(target, exp.Table) else "subquery"
            line = idx.find(
                rf"\bjoin\s+\S*{re.escape(name.split('.')[-1])}", r"\bjoin\b"
            )
            out.append(
                self.issue(
                    f"Join to {name} uses a non-equi condition "
                    f"({_truncate(on.sql(), 60)}) - each left row can match many "
                    "right rows, multiplying output and forcing a nested-loop or "
                    "range-join plan.",
                    line=line,
                    fix_suggestion=(
                        "Add an equality component to the join key, or pre-bucket "
                        "both sides so the range join is bounded."
                    ),
                )
            )
        return out

    def _double_join(self, select: exp.Select, idx: _LineIndex) -> List[Issue]:
        """Same physical table joined twice without dedup/aggregation."""
        out: List[Issue] = []
        if _has_aggregation(select):
            return out
        tables = _direct_tables(select)
        by_name: Dict[str, List[exp.Table]] = {}
        for table in tables:
            by_name.setdefault(table.name.lower(), []).append(table)
        join_conditions: Dict[int, str] = {}
        for join in select.args.get("joins") or []:
            on = join.args.get("on")
            if isinstance(join.this, exp.Table) and isinstance(on, exp.Expression):
                join_conditions[id(join.this)] = _normalized_join_condition(join.this, on)
        for name, nodes in by_name.items():
            if len(nodes) < 2:
                continue
            conditions = {join_conditions.get(id(n), f"from:{i}") for i, n in enumerate(nodes)}
            if len(conditions) < len(nodes):
                continue  # Exact duplicates are DUPLICATE_JOIN_TABLE's finding.
            display = _tname(nodes[0])
            line = idx.find(rf"\bjoin\s+\S*{re.escape(nodes[0].name)}", r"\bjoin\b")
            out.append(
                self.issue(
                    f"{display} appears {len(nodes)} times in the FROM/JOIN list "
                    "with no GROUP BY or DISTINCT afterwards - if the join keys "
                    "are non-unique the result fans out multiplicatively.",
                    line=line,
                    fix_suggestion=(
                        f"Aggregate or deduplicate {display} before joining it "
                        "twice, or combine both joins into one pass."
                    ),
                )
            )
        return out


@register
class WindowOnFullTableRule(Rule):
    """OVER () without PARTITION BY sorts/buffers the entire table."""

    id = "WINDOW_FUNCTION_ON_FULL_TABLE"
    severity = "WARNING"
    category = "performance"
    formats = SQL_DBT_FLINK
    title = "Window function over full table"
    description = (
        "A window with no PARTITION BY makes one global partition: the engine "
        "must gather (and often sort) the entire input on a single node."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag window specs lacking PARTITION BY."""
        issues: List[Issue] = []
        idx = _LineIndex(pr)
        for stmt in _statements(pr):
            for window in stmt.find_all(exp.Window):
                if window.args.get("partition_by"):
                    continue
                func_sql = _truncate(window.this.sql() if window.this else "window", 50)
                line = idx.find(r"\bover\s*\(")
                issues.append(
                    self.issue(
                        f"{func_sql} OVER (...) has no PARTITION BY - the window "
                        "spans the whole table, serializing it through a single "
                        "global sort/buffer.",
                        line=line,
                        fix_suggestion=(
                            "Add PARTITION BY on the natural entity key so the "
                            "window is computed per group in parallel."
                        ),
                        fix_diff=self._build_diff(idx, line),
                    )
                )
        return issues

    @staticmethod
    def _build_diff(idx: _LineIndex, line: Optional[int]) -> Optional[str]:
        """Insert a PARTITION BY into the OVER clause on the offending line."""
        text = idx.text(line)
        if not text:
            return None
        new_text, count = re.subn(
            r"(?i)over\s*\(\s*\)", "OVER (PARTITION BY <entity_key>)", text, count=1
        )
        if count == 0:
            new_text, count = re.subn(
                r"(?i)over\s*\(", "OVER (PARTITION BY <entity_key> ", text, count=1
            )
        if count == 0:
            return None
        return _diff(text, new_text, line)


@register
class RedundantDistinctRule(Rule):
    """DISTINCT layered on an operation that already deduplicates."""

    id = "REDUNDANT_DISTINCT"
    severity = "WARNING"
    category = "performance"
    formats = SQL_DBT
    title = "Redundant DISTINCT"
    description = (
        "DISTINCT combined with GROUP BY (or inside IN/EXISTS subqueries) "
        "pays for a deduplication the query already performs."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag DISTINCT+GROUP BY and DISTINCT inside IN subqueries."""
        issues: List[Issue] = []
        idx = _LineIndex(pr)
        for stmt in _statements(pr):
            for select in stmt.find_all(exp.Select):
                has_distinct = select.args.get("distinct") is not None
                if not has_distinct:
                    continue
                group = select.args.get("group")
                if group is not None:
                    group_sql = _truncate(group.sql(), 50)
                    line = idx.find(r"\bdistinct\b")
                    issues.append(
                        self.issue(
                            f"SELECT DISTINCT is combined with {group_sql} - the "
                            "GROUP BY already returns one row per group, so the "
                            "DISTINCT pass is pure overhead.",
                            line=line,
                            fix_suggestion="Drop the DISTINCT; GROUP BY already deduplicates.",
                        )
                    )
                    continue
                ancestor = select.find_ancestor(exp.In, exp.Exists)
                if ancestor is not None:
                    op = "IN" if isinstance(ancestor, exp.In) else "EXISTS"
                    line = idx.find(r"\bdistinct\b")
                    issues.append(
                        self.issue(
                            f"DISTINCT inside an {op} subquery is redundant - {op} "
                            "semantics already ignore duplicate matches, so the "
                            "dedup sort is wasted work.",
                            line=line,
                            fix_suggestion=f"Remove DISTINCT from the {op} subquery.",
                        )
                    )
        return issues


@register
class NotInNullRule(Rule):
    """NOT IN (subquery) silently returns zero rows when NULLs appear."""

    id = "ANTI_PATTERN_NOT_IN_NULL"
    severity = "WARNING"
    category = "reliability"
    formats = SQL_DBT
    title = "NOT IN with nullable subquery"
    description = (
        "If the subquery ever returns a NULL, x NOT IN (subquery) evaluates to "
        "NULL for every row and the whole filter returns nothing - a silent "
        "correctness bug."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag negated IN-subqueries (incl. normalized ``<> ALL`` forms)."""
        issues: List[Issue] = []
        idx = _LineIndex(pr)
        for stmt in _statements(pr):
            for in_node in stmt.find_all(exp.In):
                query = in_node.args.get("query")
                if query is None or not self._is_negated(in_node):
                    continue
                inner = query.this if isinstance(query, exp.Subquery) else query
                self._emit(in_node.this, inner, idx, issues)
            # Some dialects (e.g. snowflake) normalize NOT IN (subquery) into
            # ``x <> ALL (subquery)`` at parse time - same NULL trap.
            for neq in stmt.find_all(exp.NEQ):
                all_ = neq.expression
                if not isinstance(all_, exp.All):
                    continue
                inner = all_.this
                if isinstance(inner, exp.Subquery):
                    inner = inner.this
                self._emit(neq.this, inner, idx, issues)
        return issues

    def _emit(
        self,
        outer: Optional[exp.Expression],
        inner: exp.Expression,
        idx: _LineIndex,
        issues: List[Issue],
    ) -> None:
        """Report one unguarded NOT IN / <> ALL subquery."""
        if not isinstance(inner, exp.Select):
            return
        inner_where = inner.args.get("where")
        if inner_where is not None and self._has_not_null_guard(inner_where):
            return
        inner_col = self._first_projection(inner)
        inner_table = next(
            (_tname(t) for t in inner.find_all(exp.Table)), "the subquery"
        )
        outer_sql = outer.sql() if isinstance(outer, exp.Expression) else "the column"
        line = idx.find(r"\bnot\s+in\b", r"(!=|<>)\s*all\s*\(")
        issues.append(
            self.issue(
                f"{outer_sql} NOT IN (SELECT {inner_col} FROM {inner_table}) "
                f"returns zero rows if {inner_col} is ever NULL - NULL "
                "membership comparisons are three-valued.",
                line=line,
                fix_suggestion=(
                    "Rewrite as NOT EXISTS, or add WHERE "
                    f"{inner_col} IS NOT NULL inside the subquery."
                ),
                fix_diff=self._build_diff(idx, line, inner, inner_col, outer_sql),
            )
        )

    @staticmethod
    def _has_not_null_guard(where: exp.Expression) -> bool:
        """True when the subquery's WHERE contains an IS NOT NULL predicate."""
        for not_ in where.find_all(exp.Not):
            inner = not_.this
            if isinstance(inner, exp.Is) and isinstance(inner.expression, exp.Null):
                return True
        return "not null" in where.sql().lower()

    @staticmethod
    def _is_negated(in_node: exp.In) -> bool:
        """True for ``x NOT IN (...)`` (possibly parenthesized)."""
        parent = in_node.parent
        while isinstance(parent, exp.Paren):
            parent = parent.parent
        return isinstance(parent, exp.Not)

    @staticmethod
    def _first_projection(inner: exp.Select) -> str:
        """SQL of the subquery's first projected column."""
        if inner.expressions:
            proj = inner.expressions[0]
            node = proj.this if isinstance(proj, exp.Alias) else proj
            return node.sql() if isinstance(node, exp.Expression) else "the column"
        return "the column"

    @staticmethod
    def _build_diff(
        idx: _LineIndex,
        line: Optional[int],
        inner: exp.Select,
        inner_col: str,
        outer_sql: str,
    ) -> Optional[str]:
        """Rewrite a single-line NOT IN into NOT EXISTS."""
        text = idx.text(line)
        if not text:
            return None
        match = re.search(
            rf"(?i){re.escape(outer_sql)}\s+not\s+in\s*\((.*)\)", text
        )
        if not match or match.group(1).count("(") != match.group(1).count(")"):
            return None
        from_ = _from_clause(inner)
        if from_ is None or not isinstance(from_.this, exp.Table):
            return None
        inner_ref = (
            inner_col if "." in inner_col else f"{_alias_or_name(from_.this)}.{inner_col}"
        )
        replacement = (
            f"NOT EXISTS (SELECT 1 FROM {from_.this.sql()} "
            f"WHERE {inner_ref} = {outer_sql})"
        )
        new_text = text[: match.start()] + replacement + text[match.end():]
        return _diff(text, new_text, line)


@register
class FunctionOnFilterColumnRule(Rule):
    """Functions wrapped around filtered columns make predicates non-sargable."""

    id = "FUNCTION_ON_FILTER_COLUMN"
    severity = "WARNING"
    category = "performance"
    formats = SQL_DBT_FLINK
    title = "Function on filtered column"
    description = (
        "Applying a function to the column inside WHERE prevents the engine "
        "from using indexes, zone maps, or partition pruning on that column."
    )

    _COMPARISONS = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag WHERE comparisons whose column side is wrapped in a function."""
        issues: List[Issue] = []
        idx = _LineIndex(pr)
        for stmt in _statements(pr):
            for where in stmt.find_all(exp.Where):
                for cmp_node in where.find_all(*self._COMPARISONS):
                    issue = self._check_comparison(cmp_node, idx)
                    if issue is not None:
                        issues.append(issue)
        return issues

    def _check_comparison(
        self, cmp_node: exp.Expression, idx: _LineIndex
    ) -> Optional[Issue]:
        """Inspect one comparison for a function-wrapped column side."""
        for side, other in (
            (cmp_node.this, cmp_node.expression),
            (cmp_node.expression, cmp_node.this),
        ):
            if not isinstance(side, (exp.Func, exp.Cast)):
                continue
            col = side.find(exp.Column)
            if col is None or (isinstance(other, exp.Expression) and other.find(exp.Column)):
                continue
            func_label = (
                "CAST" if isinstance(side, exp.Cast) else type(side).__name__.upper()
            )
            line = idx.find(
                rf"\(\s*[^()]*{re.escape(col.name)}[^()]*\)",
                rf"{re.escape(col.name)}\s*::",
                rf"\b{re.escape(col.name)}\b",
            )
            return self.issue(
                f"WHERE wraps {col.sql()} in {func_label}(...) before comparing "
                f"with {_truncate(other.sql() if isinstance(other, exp.Expression) else '?', 40)} "
                "- the predicate is non-sargable, so no index or partition "
                "pruning applies.",
                line=line,
                fix_suggestion=(
                    f"Move the computation to the literal side and compare "
                    f"{col.sql()} directly (e.g. a half-open date range)."
                ),
                fix_diff=self._date_range_diff(cmp_node, side, other, col, idx, line),
            )
        return None

    @staticmethod
    def _date_range_diff(
        cmp_node: exp.Expression,
        side: exp.Expression,
        other: exp.Expression,
        col: exp.Column,
        idx: _LineIndex,
        line: Optional[int],
    ) -> Optional[str]:
        """Rewrite ``DATE(col) = 'd'`` into a sargable half-open range."""
        if not isinstance(cmp_node, exp.EQ):
            return None
        is_date_trunc = isinstance(side, (exp.Date, exp.DateTrunc)) or (
            isinstance(side, exp.Cast)
            and side.args.get("to") is not None
            and "date" in side.args["to"].sql().lower()
        )
        if not is_date_trunc:
            return None
        if not (isinstance(other, exp.Literal) and other.is_string):
            return None
        value = other.this or ""
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", value):
            return None
        try:
            next_day = (date.fromisoformat(value) + timedelta(days=1)).isoformat()
        except ValueError:
            return None
        text = idx.text(line)
        if not text:
            return None
        col_sql = col.sql()
        new_pred = f"{col_sql} >= '{value}' AND {col_sql} < '{next_day}'"
        patterns = (
            rf"(?i)[\w.]+\s*\([^()]*\)\s*=\s*'{re.escape(value)}'",
            rf"(?i){re.escape(col_sql)}\s*::\s*date\s*=\s*'{re.escape(value)}'",
        )
        for pattern in patterns:
            new_text, count = re.subn(pattern, new_pred, text, count=1)
            if count:
                return _diff(text, new_text, line)
        return None


@register
class UnionInsteadOfUnionAllRule(Rule):
    """UNION deduplicates; UNION ALL is free when duplicates are impossible."""

    id = "UNION_INSTEAD_OF_UNION_ALL"
    severity = "WARNING"
    category = "performance"
    formats = SQL_DBT_FLINK
    title = "UNION instead of UNION ALL"
    description = (
        "Plain UNION sorts/hashes the combined result to drop duplicates. If "
        "the branches are disjoint (or duplicates are fine), UNION ALL skips "
        "the most expensive step."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag every distinct UNION (not EXCEPT/INTERSECT/UNION ALL)."""
        issues: List[Issue] = []
        idx = _LineIndex(pr)
        for stmt in _statements(pr):
            for union in stmt.find_all(exp.Union):
                if type(union) is not exp.Union or not union.args.get("distinct"):
                    continue
                line = idx.find(r"\bunion\b(?!\s+all)")
                text = idx.text(line)
                fix_diff = None
                if text:
                    new_text, count = re.subn(
                        r"(?i)\bunion\b(?!\s+all)", "UNION ALL", text, count=1
                    )
                    if count:
                        fix_diff = _diff(text, new_text, line)
                issues.append(
                    self.issue(
                        "UNION deduplicates the combined result with a full "
                        "sort/hash - if the branches cannot overlap (or duplicates "
                        "are acceptable) this cost is pure overhead.",
                        line=line,
                        fix_suggestion="Use UNION ALL when duplicate rows are impossible or acceptable.",
                        fix_diff=fix_diff,
                    )
                )
        return issues


@register
class MissingIndexHintRule(Rule):
    """Selective filters on large joined tables with no index consideration."""

    id = "MISSING_INDEX_HINT"
    severity = "WARNING"
    category = "performance"
    formats = SQL_DBT
    title = "Possible missing index"
    description = (
        "On Postgres/Redshift, a selective equality filter on a large joined "
        "table with no index (or sort/dist key) note suggests the scan is "
        "unindexed - a common silent slowdown."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Conservatively flag one likely-unindexed selective filter per statement."""
        issues: List[Issue] = []
        dialect = (pr.ir.dialect or "").lower()
        if dialect not in {"postgres", "redshift"}:
            return issues
        if "index" in pr.source.lower() or re.search(
            r"(?i)\b(sortkey|distkey)\b", pr.source
        ):
            return issues
        idx = _LineIndex(pr)
        for stmt in _statements(pr):
            for select in stmt.find_all(exp.Select):
                joins = select.args.get("joins") or []
                if not joins:
                    continue
                alias_map = {alias: src for alias, src in _direct_sources(select)}
                where = select.args.get("where")
                if not isinstance(where, exp.Where):
                    continue
                found = self._selective_filter(where, alias_map)
                if found is None:
                    continue
                col, table = found
                line = idx.find(
                    rf"{re.escape(col.sql())}\s*=", rf"\b{re.escape(col.name)}\b"
                )
                issues.append(
                    self.issue(
                        f"Equality filter on {col.sql()} targets the "
                        f"large-looking joined table {_tname(table)} and the "
                        "script never mentions an index/sortkey - on "
                        f"{dialect} this likely degrades to a sequential scan.",
                        line=line,
                        fix_suggestion=(
                            f"Verify an index (or SORTKEY) exists on "
                            f"{table.name}.{col.name}; add one or document it in "
                            "a comment."
                        ),
                    )
                )
                break  # one conservative finding per parse
        return issues

    @staticmethod
    def _selective_filter(
        where: exp.Where, alias_map: Dict[str, exp.Expression]
    ) -> Optional[Tuple[exp.Column, exp.Table]]:
        """First col = literal filter aimed at a large-looking joined table."""
        for eq in where.find_all(exp.EQ):
            for side, other in ((eq.this, eq.expression), (eq.expression, eq.this)):
                if not (isinstance(side, exp.Column) and isinstance(other, exp.Literal)):
                    continue
                qualifier = side.table.lower()
                source = alias_map.get(qualifier)
                if isinstance(source, exp.Table) and _LARGE_TABLE_RX.search(source.name):
                    return side, source
        return None


@register
class OrderByWithoutLimitRule(Rule):
    """A global ORDER BY without LIMIT sorts the entire result for nothing."""

    id = "ORDER_BY_WITHOUT_LIMIT"
    severity = "WARNING"
    category = "performance"
    formats = SQL_DBT
    title = "ORDER BY without LIMIT"
    description = (
        "A top-level ORDER BY with no LIMIT forces a full global sort whose "
        "ordering is discarded by most consumers (tables have no order)."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag top-level SELECT/UNION statements ordered but unbounded."""
        issues: List[Issue] = []
        if pr.ir.materialization.type == "view":
            return issues
        idx = _LineIndex(pr)
        for stmt in _statements(pr):
            if not isinstance(stmt, (exp.Select, exp.Union)):
                continue
            order = stmt.args.get("order")
            if order is None or stmt.args.get("limit") is not None:
                continue
            order_sql = _truncate(order.sql(), 60)
            line = idx.find_last(r"\border\s+by\b")
            text = idx.text(line)
            fix_diff = (
                _diff(
                    text,
                    [text, f"{_indent(text)}LIMIT 1000  -- bound the sort, or drop the ORDER BY"],
                    line,
                )
                if text
                else None
            )
            issues.append(
                self.issue(
                    f"Top-level {order_sql} has no LIMIT - the engine sorts the "
                    "entire result set even though inserted/loaded data keeps no "
                    "order.",
                    line=line,
                    fix_suggestion=(
                        "Add a LIMIT if you need the top rows, or remove the "
                        "ORDER BY and sort in the consumer."
                    ),
                    fix_diff=fix_diff,
                )
            )
        return issues


@register
class ImplicitColumnInsertRule(Rule):
    """INSERT without a column list breaks on any schema change."""

    id = "IMPLICIT_COLUMN_INSERT"
    severity = "WARNING"
    category = "reliability"
    formats = SQL_FLINK
    title = "INSERT without column list"
    description = (
        "INSERT INTO t SELECT ... binds values by position; adding or "
        "reordering a column in either side silently corrupts data or fails."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag INSERT statements whose target has no explicit column list."""
        issues: List[Issue] = []
        idx = _LineIndex(pr)
        for stmt in _statements(pr):
            for insert in stmt.find_all(exp.Insert):
                target = insert.this
                if not isinstance(target, exp.Table):
                    continue  # exp.Schema target == explicit column list.
                name = _tname(target)
                line = idx.find(
                    rf"insert\s+(into|overwrite)\s+\S*{re.escape(target.name)}",
                    r"\binsert\b",
                )
                issues.append(
                    self.issue(
                        f"INSERT INTO {name} has no column list - values bind by "
                        "position, so any schema change in "
                        f"{name} or the source silently misaligns columns.",
                        line=line,
                        fix_suggestion=(
                            f"Spell out the target columns: INSERT INTO {name} "
                            "(col_a, col_b, ...) SELECT ..."
                        ),
                    )
                )
        return issues


@register
class CrossDatabaseJoinRule(Rule):
    """Joins across catalogs/databases often move data between systems."""

    id = "CROSS_DATABASE_JOIN"
    severity = "WARNING"
    category = "performance"
    formats = SQL_DBT
    title = "Cross-database join"
    description = (
        "Joining tables from different databases/catalogs prevents co-located "
        "joins and may pull one side across the network in full."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag selects whose direct sources span 2+ catalogs."""
        issues: List[Issue] = []
        idx = _LineIndex(pr)
        for stmt in _statements(pr):
            for select in stmt.find_all(exp.Select):
                tables = [t for t in _direct_tables(select) if t.catalog]
                catalogs = {t.catalog.lower() for t in tables}
                if len(catalogs) < 2:
                    continue
                names = sorted({_tname(t) for t in tables})
                line = idx.find(rf"\b{re.escape(tables[-1].catalog)}\.", r"\bjoin\b")
                issues.append(
                    self.issue(
                        f"Query joins across databases {', '.join(sorted(catalogs))} "
                        f"({', '.join(names[:4])}) - the engine cannot co-locate "
                        "the join and may ship a full table between databases.",
                        line=line,
                        fix_suggestion=(
                            "Replicate/stage the smaller table into the primary "
                            "database before joining."
                        ),
                    )
                )
        return issues


@register
class ScalarUdfInPredicateRule(Rule):
    """UDFs in WHERE run row-by-row and block vectorization and pruning."""

    id = "SCALAR_UDF_IN_PREDICATE"
    severity = "WARNING"
    category = "performance"
    formats = SQL_DBT
    title = "Scalar UDF in predicate"
    description = (
        "User-defined (or unrecognized) functions in WHERE are evaluated "
        "row-by-row, cannot be pruned, and often disable vectorized execution."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag unrecognized function calls inside WHERE clauses (max 3/stmt)."""
        issues: List[Issue] = []
        idx = _LineIndex(pr)
        for stmt in _statements(pr):
            per_stmt = 0
            seen: Set[str] = set()
            for where in stmt.find_all(exp.Where):
                for anon in where.find_all(exp.Anonymous):
                    name = str(anon.this) if anon.this else ""
                    if not name or name.lower() in seen or per_stmt >= 3:
                        continue
                    seen.add(name.lower())
                    per_stmt += 1
                    cols = [c.sql() for c in anon.find_all(exp.Column)][:2]
                    line = idx.find(rf"\b{re.escape(name)}\s*\(")
                    issues.append(
                        self.issue(
                            f"WHERE calls {name}({', '.join(cols) if cols else '...'}) "
                            "- a UDF/unrecognized function evaluated per row that "
                            "blocks predicate pushdown and pruning.",
                            line=line,
                            fix_suggestion=(
                                f"Precompute {name}(...) into a column upstream, "
                                "or rewrite the predicate with built-in functions."
                            ),
                        )
                    )
        return issues


@register
class DuplicateJoinTableRule(Rule):
    """Identical join (same table, same condition) repeated in one query."""

    id = "DUPLICATE_JOIN_TABLE"
    severity = "WARNING"
    category = "performance"
    formats = SQL_DBT
    title = "Duplicate join"
    description = (
        "Joining the same table twice with an identical ON condition does the "
        "same work twice and usually signals a copy-paste error."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag joins repeated with the same table and normalized condition."""
        issues: List[Issue] = []
        idx = _LineIndex(pr)
        for stmt in _statements(pr):
            for select in stmt.find_all(exp.Select):
                seen: Dict[Tuple[str, str], int] = {}
                for join in select.args.get("joins") or []:
                    target = join.this
                    on = join.args.get("on")
                    if not isinstance(target, exp.Table) or not isinstance(on, exp.Expression):
                        continue
                    key = (
                        target.name.lower(),
                        _normalized_join_condition(target, on),
                    )
                    seen[key] = seen.get(key, 0) + 1
                    if seen[key] == 2:
                        line = idx.find(rf"\bjoin\s+\S*{re.escape(target.name)}")
                        issues.append(
                            self.issue(
                                f"{_tname(target)} is joined twice with the exact "
                                f"same condition ({_truncate(on.sql(), 50)}) - the "
                                "second join repeats identical work.",
                                line=line,
                                fix_suggestion=(
                                    f"Remove the duplicate join of {target.name} and "
                                    "reuse the first join's alias."
                                ),
                            )
                        )
        return issues


@register
class DistinctStarRule(Rule):
    """SELECT DISTINCT * deduplicates across every column of the source."""

    id = "DISTINCT_STAR"
    severity = "WARNING"
    category = "performance"
    formats = SQL_DBT
    title = "SELECT DISTINCT *"
    description = (
        "DISTINCT over every column hashes the entire row for the whole table "
        "and usually papers over a join fan-out instead of fixing it."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag selects that are exactly DISTINCT *."""
        issues: List[Issue] = []
        idx = _LineIndex(pr)
        for stmt in _statements(pr):
            for select in stmt.find_all(exp.Select):
                if select.args.get("distinct") is None:
                    continue
                if len(select.expressions) != 1 or not isinstance(
                    select.expressions[0], exp.Star
                ):
                    continue
                tables = [_tname(t) for t in _direct_tables(select)] or ["the source"]
                line = idx.find(r"select\s+distinct\s+\*", r"\bdistinct\b")
                issues.append(
                    self.issue(
                        f"SELECT DISTINCT * over {', '.join(tables)} hashes every "
                        "column of every row to deduplicate - if duplicates exist "
                        "they usually come from an upstream join fan-out.",
                        line=line,
                        fix_suggestion=(
                            "Fix the join that produces duplicates (or dedupe on "
                            "the business key with ROW_NUMBER) instead of DISTINCT *."
                        ),
                    )
                )
        return issues


@register
class HavingWithoutAggregateRule(Rule):
    """HAVING without aggregates filters too late - after aggregation."""

    id = "HAVING_WITHOUT_AGGREGATE"
    severity = "WARNING"
    category = "performance"
    formats = SQL_DBT_FLINK
    title = "HAVING without aggregate"
    description = (
        "A HAVING predicate that uses no aggregate could run in WHERE instead, "
        "filtering rows before the expensive aggregation."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag HAVING clauses containing no aggregate function."""
        issues: List[Issue] = []
        idx = _LineIndex(pr)
        for stmt in _statements(pr):
            for select in stmt.find_all(exp.Select):
                having = select.args.get("having")
                if having is None or having.find(exp.AggFunc) is not None:
                    continue
                cond = _truncate(having.this.sql() if having.this else "", 60)
                line = idx.find(r"\bhaving\b")
                issues.append(
                    self.issue(
                        f"HAVING {cond} references no aggregate - the filter runs "
                        "after GROUP BY, so every group is computed first and "
                        "then discarded.",
                        line=line,
                        fix_suggestion=f"Move the predicate into WHERE: WHERE {cond}.",
                    )
                )
        return issues


@register
class NullComparisonRule(Rule):
    """``= NULL`` / ``!= NULL`` never match - NULL needs IS [NOT] NULL."""

    id = "NULL_COMPARISON"
    severity = "WARNING"
    category = "reliability"
    formats = SQL_DBT_FLINK
    title = "Comparison with = NULL"
    description = (
        "Comparing with = NULL or != NULL yields NULL (never TRUE) under "
        "three-valued logic, so the predicate silently filters everything out."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag equality/inequality comparisons against the NULL keyword."""
        issues: List[Issue] = []
        idx = _LineIndex(pr)
        for stmt in _statements(pr):
            for cmp_node in stmt.find_all(exp.EQ, exp.NEQ):
                if not isinstance(cmp_node.expression, exp.Null) and not isinstance(
                    cmp_node.this, exp.Null
                ):
                    continue
                other = (
                    cmp_node.this
                    if isinstance(cmp_node.expression, exp.Null)
                    else cmp_node.expression
                )
                other_sql = other.sql() if isinstance(other, exp.Expression) else "expr"
                is_eq = isinstance(cmp_node, exp.EQ)
                wanted = "IS NULL" if is_eq else "IS NOT NULL"
                line = idx.find(r"(=|!=|<>)\s*null\b")
                text = idx.text(line)
                fix_diff = None
                if text:
                    pattern = r"(?i)\s*=\s*null\b" if is_eq else r"(?i)\s*(!=|<>)\s*null\b"
                    new_text, count = re.subn(pattern, f" {wanted}", text, count=1)
                    if count:
                        fix_diff = _diff(text, new_text, line)
                issues.append(
                    self.issue(
                        f"{other_sql} {'=' if is_eq else '!='} NULL never evaluates "
                        f"to TRUE - use {other_sql} {wanted} instead.",
                        line=line,
                        fix_suggestion=f"Replace with {other_sql} {wanted}.",
                        fix_diff=fix_diff,
                    )
                )
        return issues


# ---------------------------------------------------------------------------
# INFO rules
# ---------------------------------------------------------------------------

@register
class MissingColumnAliasRule(Rule):
    """Computed projections without AS get engine-generated names."""

    id = "MISSING_COLUMN_ALIAS"
    severity = "INFO"
    category = "maintainability"
    formats = SQL_DBT_FLINK
    title = "Computed column without alias"
    description = (
        "Expressions in the SELECT list without an explicit alias receive "
        "unstable engine-generated names that break downstream consumers."
    )

    _MAX_FINDINGS = 5

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag unaliased computed projections (capped to avoid noise)."""
        issues: List[Issue] = []
        idx = _LineIndex(pr)
        for stmt in _statements(pr):
            for select in stmt.find_all(exp.Select):
                if _in_exists(select):
                    continue
                for proj in select.expressions:
                    if len(issues) >= self._MAX_FINDINGS:
                        return issues
                    if isinstance(proj, (exp.Alias, exp.Column, exp.Star)):
                        continue
                    if not isinstance(
                        proj, (exp.Func, exp.Binary, exp.Case, exp.Cast, exp.Paren)
                    ):
                        continue
                    snippet = _truncate(proj.sql(), 60)
                    col = proj.find(exp.Column)
                    line = idx.find(
                        rf"\b{re.escape(col.name)}\b" if col is not None else "",
                        r"\bselect\b",
                    )
                    issues.append(
                        self.issue(
                            f"Projection {snippet} has no AS alias - its output "
                            "name is engine-generated and unstable across "
                            "dialects and versions.",
                            line=line,
                            fix_suggestion=f"Name it explicitly: {snippet} AS <column_name>.",
                        )
                    )
        return issues


@register
class HardcodedDateRule(Rule):
    """Date literals in predicates rot silently as time passes."""

    id = "HARDCODED_DATE"
    severity = "INFO"
    category = "maintainability"
    formats = SQL_DBT_FLINK
    title = "Hardcoded date literal"
    description = (
        "A literal date in a filter stops being correct the day after it is "
        "written; parameterize it or derive it from the run date."
    )

    _MAX_FINDINGS = 5

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag date-shaped string literals used inside predicates."""
        issues: List[Issue] = []
        idx = _LineIndex(pr)
        seen: Set[str] = set()
        for stmt in _statements(pr):
            for lit in stmt.find_all(exp.Literal):
                if len(issues) >= self._MAX_FINDINGS:
                    return issues
                if not lit.is_string or not _DATE_LITERAL_RX.match(lit.this or ""):
                    continue
                if lit.find_ancestor(exp.Where, exp.Join, exp.Having, exp.Qualify) is None:
                    continue
                if lit.this in seen:
                    continue
                seen.add(lit.this)
                col = None
                parent = lit.parent
                if isinstance(parent, exp.Binary):
                    sibling = parent.this if parent.expression is lit else parent.expression
                    col = sibling.sql() if isinstance(sibling, exp.Expression) else None
                line = idx.find(rf"'{re.escape(lit.this)}'")
                issues.append(
                    self.issue(
                        f"Predicate compares {col or 'a column'} to the hardcoded "
                        f"date '{lit.this}' - the filter silently goes stale.",
                        line=line,
                        fix_suggestion=(
                            "Derive the bound from the run date (CURRENT_DATE "
                            "arithmetic) or a parameter/dbt var instead of a literal."
                        ),
                    )
                )
        return issues


@register
class InconsistentCaseRule(Rule):
    """Mixed UPPER/lower keyword casing hurts readability and reviews."""

    id = "INCONSISTENT_CASE_CONVENTION"
    severity = "INFO"
    category = "maintainability"
    formats = SQL_DBT_FLINK
    title = "Inconsistent keyword casing"
    description = (
        "The script mixes UPPERCASE and lowercase SQL keywords; a single "
        "convention keeps diffs and reviews clean."
    )

    _KEYWORD_RX = re.compile(
        r"\b(select|from|where|join|group|order|having|union|insert|update"
        r"|delete|distinct|case|when|with)\b",
        re.IGNORECASE,
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag once when both casing styles appear at least twice each."""
        if not _statements(pr):
            return []
        upper = lower = 0
        first_upper_line: Optional[int] = None
        first_lower_line: Optional[int] = None
        upper_example = lower_example = ""
        for i, raw in enumerate(pr.lines, start=1):
            text = re.sub(r"--.*$", "", re.sub(r"'[^']*'", "''", raw))
            for match in self._KEYWORD_RX.finditer(text):
                token = match.group(0)
                if token.isupper():
                    upper += 1
                    if first_upper_line is None:
                        first_upper_line, upper_example = i, token
                elif token.islower():
                    lower += 1
                    if first_lower_line is None:
                        first_lower_line, lower_example = i, token
        if upper < 2 or lower < 2:
            return []
        minority_line = first_lower_line if upper >= lower else first_upper_line
        return [
            self.issue(
                f"Keywords mix casing: {upper} UPPERCASE (e.g. {upper_example}) "
                f"vs {lower} lowercase (e.g. {lower_example}) - pick one "
                "convention for the whole script.",
                line=minority_line,
                fix_suggestion=(
                    "Run the script through a formatter (e.g. sqlfmt/sqlfluff) "
                    "to enforce a single keyword case."
                ),
            )
        ]


@register
class GroupByOrdinalRule(Rule):
    """GROUP BY ordinals silently re-bind when the SELECT list changes."""

    id = "GROUP_BY_ORDINAL"
    severity = "INFO"
    category = "maintainability"
    formats = SQL_DBT
    title = "GROUP BY ordinal"
    description = (
        "GROUP BY 1, 2 refers to projection positions; reordering the SELECT "
        "list silently changes the grouping semantics."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag GROUP BY clauses that use positional ordinals."""
        issues: List[Issue] = []
        idx = _LineIndex(pr)
        for stmt in _statements(pr):
            for select in stmt.find_all(exp.Select):
                group = select.args.get("group")
                if not isinstance(group, exp.Group):
                    continue
                ordinals = [
                    e for e in group.expressions
                    if isinstance(e, exp.Literal) and e.is_number
                ]
                if not ordinals:
                    continue
                resolved: List[str] = []
                for ordinal in ordinals:
                    try:
                        pos = int(ordinal.this)
                        proj = select.expressions[pos - 1]
                        resolved.append(
                            proj.alias if isinstance(proj, exp.Alias) and proj.alias
                            else _truncate(proj.sql(), 30)
                        )
                    except (ValueError, IndexError):
                        resolved.append("?")
                line = idx.find(r"\bgroup\s+by\b")
                ord_list = ", ".join(o.this for o in ordinals)
                issues.append(
                    self.issue(
                        f"GROUP BY {ord_list} uses positional ordinals (currently "
                        f"{', '.join(resolved)}) - reordering the SELECT list "
                        "silently changes the grouping.",
                        line=line,
                        fix_suggestion=f"Group by explicit expressions: GROUP BY {', '.join(resolved)}.",
                    )
                )
        return issues


@register
class CaseWithoutElseRule(Rule):
    """CASE with no ELSE yields NULL for unmatched rows - often unintended."""

    id = "CASE_WITHOUT_ELSE"
    severity = "INFO"
    category = "reliability"
    formats = SQL_DBT_FLINK
    title = "CASE without ELSE"
    description = (
        "A CASE expression without ELSE returns NULL whenever no branch "
        "matches, silently injecting NULLs into downstream data."
    )

    _MAX_FINDINGS = 5

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag CASE expressions lacking an ELSE branch (capped)."""
        issues: List[Issue] = []
        idx = _LineIndex(pr)
        for stmt in _statements(pr):
            for case in stmt.find_all(exp.Case):
                if len(issues) >= self._MAX_FINDINGS:
                    return issues
                if case.args.get("default") is not None:
                    continue
                parent = case.parent
                label = (
                    f" (column {parent.alias})"
                    if isinstance(parent, exp.Alias) and parent.alias
                    else ""
                )
                line = idx.find(r"\bcase\b")
                issues.append(
                    self.issue(
                        f"CASE expression{label} has no ELSE - rows matching no "
                        "WHEN branch become NULL silently.",
                        line=line,
                        fix_suggestion=(
                            "Add an explicit ELSE (e.g. ELSE 'unknown' or ELSE 0) "
                            "to make the fallback intentional."
                        ),
                    )
                )
        return issues


@register
class UnusedCteRule(Rule):
    """A defined-but-never-referenced CTE is dead code (and may still run)."""

    id = "UNUSED_CTE"
    severity = "INFO"
    category = "maintainability"
    formats = SQL_DBT
    title = "Unused CTE"
    description = (
        "A CTE that is never referenced is dead code; some engines still "
        "evaluate it, paying for a result nobody reads."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag CTEs with zero references outside their own definition."""
        issues: List[Issue] = []
        idx = _LineIndex(pr)
        for stmt in _statements(pr):
            for cte in stmt.find_all(exp.CTE):
                name = cte.alias_or_name
                if not name:
                    continue
                referenced = any(
                    table.name.lower() == name.lower() and not _is_within(table, cte)
                    for table in stmt.find_all(exp.Table)
                )
                if referenced:
                    continue
                line = idx.find(
                    rf"\b{re.escape(name)}\s+as\s*\(", rf"\b{re.escape(name)}\b"
                )
                issues.append(
                    self.issue(
                        f"CTE {name} is defined but never referenced - dead code "
                        "that some engines still evaluate.",
                        line=line,
                        fix_suggestion=f"Delete the unused CTE {name}.",
                    )
                )
        return issues


@register
class OrderByInSubqueryRule(Rule):
    """ORDER BY inside a subquery/CTE without LIMIT is wasted work."""

    id = "ORDER_BY_IN_SUBQUERY"
    severity = "INFO"
    category = "performance"
    formats = SQL_DBT
    title = "ORDER BY in subquery"
    description = (
        "Sorting inside a derived table or CTE without LIMIT is discarded by "
        "the outer query - the sort cost buys nothing."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag nested selects ordered without LIMIT."""
        issues: List[Issue] = []
        idx = _LineIndex(pr)
        for stmt in _statements(pr):
            for select in stmt.find_all(exp.Select):
                container = select.find_ancestor(exp.Subquery, exp.CTE)
                if container is None:
                    continue
                if select.args.get("order") is None or select.args.get("limit") is not None:
                    continue
                where_label = (
                    f"CTE {container.alias_or_name}"
                    if isinstance(container, exp.CTE)
                    else f"subquery {container.alias}" if container.alias else "a subquery"
                )
                order_sql = _truncate(select.args["order"].sql(), 50)
                line = idx.find(r"\border\s+by\b")
                issues.append(
                    self.issue(
                        f"{order_sql} inside {where_label} has no LIMIT - the "
                        "outer query gives no ordering guarantee, so the sort is "
                        "pure wasted compute.",
                        line=line,
                        fix_suggestion=(
                            "Remove the inner ORDER BY (or pair it with LIMIT for "
                            "a top-N pattern)."
                        ),
                    )
                )
        return issues


@register
class LikeWithoutWildcardRule(Rule):
    """LIKE with no wildcard is just a slower, vaguer equality check."""

    id = "LIKE_WITHOUT_WILDCARD"
    severity = "INFO"
    category = "performance"
    formats = SQL_DBT_FLINK
    title = "LIKE without wildcard"
    description = (
        "A LIKE pattern containing no % or _ behaves as equality but may skip "
        "index/pruning fast paths on some engines."
    )

    _MAX_FINDINGS = 5

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag LIKE/ILIKE patterns with no wildcard characters (capped)."""
        issues: List[Issue] = []
        idx = _LineIndex(pr)
        for stmt in _statements(pr):
            for like in stmt.find_all(exp.Like, exp.ILike):
                if len(issues) >= self._MAX_FINDINGS:
                    return issues
                pattern = like.expression
                if not (isinstance(pattern, exp.Literal) and pattern.is_string):
                    continue
                value = pattern.this or ""
                if not value or any(ch in value for ch in "%_\\"):
                    continue
                column = _truncate(like.this.sql(), 40)
                op = "ILIKE" if isinstance(like, exp.ILike) else "LIKE"
                line = idx.find(rf"i?like\s+'{re.escape(value)}'", r"\bi?like\b")
                issues.append(
                    self.issue(
                        f"{column} {op} '{value}' contains no wildcard - it is an "
                        "equality test in disguise that may skip index fast paths.",
                        line=line,
                        fix_suggestion=f"Use {column} = '{value}' instead of {op}.",
                    )
                )
        return issues
