"""Static AST parsers for Airflow DAGs and Prefect/Dagster flows.

Both parsers analyze Python source with the stdlib :mod:`ast` module only —
user code is never imported, executed or evaluated. They emit the unified IR
consumed by the deterministic rule engine:

- ``DAG`` operations describing the pipeline container (DAG / flow / job),
- ``TASK`` / ``SENSOR`` operations per operator instantiation,
- ``XCOM`` operations for cross-task data passing,
- ``DYNAMIC_DAG`` markers for loop-generated DAGs/operators,
- ``triggers`` dependency edges between task ids.
"""
from __future__ import annotations

import ast
import logging
import re
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from app.schemas.ir import (
    IR,
    Dependency,
    IRMetadata,
    Location,
    Operation,
    ParseError,
    ParseResult,
    Scheduling,
)

logger = logging.getLogger(__name__)

#: Operator class-name fragments that indicate a heavy compute workload.
_HEAVY_OPERATOR_RE = re.compile(
    r"(Spark|Databricks|BigQuery|EMR|Dataproc|KubernetesPod|DataflowPython)",
    re.IGNORECASE,
)
#: Function-name fragments that indicate heavy compute (Prefect/Dagster/TaskFlow).
_HEAVY_NAME_RE = re.compile(
    r"(spark|databricks|bigquery|emr|dataproc|kubernetes|dataflow)", re.IGNORECASE
)
#: Variable names that hint at a DataFrame-sized XCom payload.
_LARGE_NAME_RE = re.compile(r"(^|_)(df|dataframe|data_frame)s?($|_|\d)", re.IGNORECASE)
#: Method calls whose result is typically too large for XCom.
_LARGE_CALL_ATTRS = frozenset(
    {
        "to_dict",
        "to_json",
        "to_records",
        "tolist",
        "to_list",
        "read",
        "readlines",
        "read_csv",
        "read_parquet",
        "read_json",
        "read_sql",
        "toPandas",
        "collect",
        "fetchall",
    }
)
#: Airflow schedule presets mapped to plain cron expressions.
_CRON_PRESETS: Dict[str, str] = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
    "@quarterly": "0 0 1 */3 *",
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
}
_TEMPLATE_PULL_RE = re.compile(r"xcom_pull\s*\((?:[^)]*task_ids\s*=\s*['\"]([^'\"]+)['\"])?")


# ---------------------------------------------------------------------------
# Shared AST helpers
# ---------------------------------------------------------------------------

def _parse_python_module(source: str, what: str) -> ast.Module:
    """Parse Python ``source`` or raise :class:`ParseError` with the line number."""
    try:
        return ast.parse(source)
    except SyntaxError as exc:
        raise ParseError(
            f"Python syntax error in {what} source: {exc.msg}", line=exc.lineno or 1
        ) from exc


def _terminal_name(node: Optional[ast.AST]) -> str:
    """The trailing identifier of a Name/Attribute chain (``a.b.C`` -> ``C``)."""
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _root_name(node: Optional[ast.AST]) -> str:
    """The leading identifier of a Name/Attribute/Call chain (``a.b.c()`` -> ``a``)."""
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


def _kwargs_map(call: Optional[ast.Call]) -> Dict[str, ast.AST]:
    """Keyword arguments of a call as ``{name: value_node}`` (ignores ``**kwargs``)."""
    if not isinstance(call, ast.Call):
        return {}
    return {kw.arg: kw.value for kw in call.keywords if kw.arg}


def _literal(node: Optional[ast.AST]) -> Any:
    """Best-effort literal evaluation of an AST node; ``None`` when not literal."""
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
        return int(value)
    return value if isinstance(value, int) else None


def _explicit_bool(node: Optional[ast.AST]) -> bool:
    """A bool literal value; non-literal (but present) values resolve to False."""
    value = _literal(node)
    return value if isinstance(value, bool) else False


def _str_list(node: Optional[ast.AST]) -> List[str]:
    """A list/tuple literal of strings, or an empty list."""
    value = _literal(node)
    if isinstance(value, (list, tuple)):
        return [v for v in value if isinstance(v, str)]
    return []


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
    if not values:
        return 0.0
    return (
        values.get("weeks", 0.0) * 7 * 1440
        + values.get("days", 0.0) * 1440
        + values.get("hours", 0.0) * 60
        + values.get("minutes", 0.0)
        + values.get("seconds", 0.0) / 60
        + values.get("milliseconds", 0.0) / 60000
        + values.get("microseconds", 0.0) / 60_000_000
    )


def _seconds_value(node: Optional[ast.AST]) -> Optional[float]:
    """A duration in seconds from either a numeric literal or a timedelta call."""
    value = _literal(node)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    minutes = _timedelta_minutes(node)
    return minutes * 60 if minutes is not None else None


def _large_data_hint(node: Optional[ast.AST]) -> bool:
    """Heuristic: does this expression look like a large (DataFrame-ish) payload?"""
    if node is None:
        return False
    for sub in ast.walk(node):
        if isinstance(sub, (ast.ListComp, ast.DictComp, ast.SetComp, ast.GeneratorExp)):
            return True
        if isinstance(sub, ast.Call) and _terminal_name(sub.func) in _LARGE_CALL_ATTRS:
            return True
        if isinstance(sub, ast.Name) and _LARGE_NAME_RE.search(sub.id):
            return True
    return False


def _location(node: ast.AST) -> Location:
    """1-based source location for an AST node."""
    return Location(
        line=int(getattr(node, "lineno", 0) or 0),
        col=int(getattr(node, "col_offset", 0) or 0),
    )


def _schedule_info(
    node: Optional[ast.AST],
) -> Tuple[Optional[str], Optional[str], Optional[int]]:
    """Normalize a schedule kwarg node into ``(schedule_str, cron, interval_minutes)``."""
    if node is None:
        return None, None, None
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str):
            text = node.value.strip()
            cron = _CRON_PRESETS.get(text)
            if cron is None and not text.startswith("@") and 5 <= len(text.split()) <= 7:
                cron = text
            return text, cron, None
        return None, None, None
    minutes = _timedelta_minutes(node)
    if minutes is not None:
        whole = max(1, int(round(minutes)))
        return f"timedelta(minutes={whole})", None, whole
    try:
        return ast.unparse(node), None, None
    except Exception:  # noqa: BLE001 - unparse failure must not kill parsing
        return None, None, None


def _call_graph_edges(
    fn: ast.AST, task_names: Set[str]
) -> Tuple[List[Tuple[str, str]], List[str]]:
    """Dataflow edges between task invocations inside a flow/job function body.

    A task call whose argument is (derived from) the result of another task
    call yields an edge ``(producer, consumer)``. Returns ``(edges, invoked)``
    where ``invoked`` lists distinct tasks called in the body, in order.
    """
    producers: Dict[str, str] = {}
    edges: List[Tuple[str, str]] = []
    invoked: List[str] = []
    seen_invoked: Set[str] = set()

    def task_of(call: ast.Call) -> Optional[str]:
        func = call.func
        if isinstance(func, ast.Name) and func.id in task_names:
            return func.id
        if (
            isinstance(func, ast.Attribute)
            and func.attr in {"submit", "map", "with_options"}
            and isinstance(func.value, ast.Name)
            and func.value.id in task_names
        ):
            return func.value.id
        return None

    def producers_of(node: ast.AST) -> List[str]:
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            out: List[str] = []
            for elt in node.elts:
                out.extend(producers_of(elt))
            return out
        producer = resolve(node)
        return [producer] if producer else []

    def resolve(node: Optional[ast.AST]) -> Optional[str]:
        if isinstance(node, ast.Name):
            return producers.get(node.id)
        if isinstance(node, ast.Await):
            return resolve(node.value)
        if isinstance(node, ast.Call):
            return handle_call(node)
        if isinstance(node, (ast.Attribute, ast.Subscript)):
            return resolve(node.value)
        return None

    def handle_call(call: ast.Call) -> Optional[str]:
        upstream: List[str] = []
        for arg in list(call.args) + [kw.value for kw in call.keywords]:
            upstream.extend(producers_of(arg))
        target = task_of(call)
        if target:
            if target not in seen_invoked:
                seen_invoked.add(target)
                invoked.append(target)
            for producer in upstream:
                if producer != target:
                    edges.append((producer, target))
            return target
        if isinstance(call.func, ast.Attribute):
            return resolve(call.func.value)
        return None

    def process(stmts: Sequence[ast.stmt]) -> None:
        for stmt in stmts:
            if isinstance(stmt, ast.Assign):
                producer = resolve(stmt.value)
                if producer:
                    for target in stmt.targets:
                        names = (
                            target.elts
                            if isinstance(target, (ast.Tuple, ast.List))
                            else [target]
                        )
                        for name in names:
                            if isinstance(name, ast.Name):
                                producers[name.id] = producer
            elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
                producer = resolve(stmt.value)
                if producer and isinstance(stmt.target, ast.Name):
                    producers[stmt.target.id] = producer
            elif isinstance(stmt, ast.AugAssign):
                resolve(stmt.value)
            elif isinstance(stmt, ast.Expr):
                resolve(stmt.value)
            elif isinstance(stmt, ast.Return) and stmt.value is not None:
                resolve(stmt.value)
            if isinstance(stmt, (ast.For, ast.AsyncFor)):
                resolve(stmt.iter)
            if isinstance(stmt, (ast.With, ast.AsyncWith)):
                for item in stmt.items:
                    resolve(item.context_expr)
            for attr in ("body", "orelse", "finalbody"):
                inner = getattr(stmt, attr, None)
                if inner:
                    process(inner)
            for handler in getattr(stmt, "handlers", None) or []:
                process(handler.body)

    body = getattr(fn, "body", None) or []
    process(body)
    return edges, invoked


# ---------------------------------------------------------------------------
# Airflow
# ---------------------------------------------------------------------------

class _AirflowCollector(ast.NodeVisitor):
    """Single-pass collector of every Airflow construct the IR needs."""

    def __init__(self) -> None:
        self.dag_calls: List[Tuple[ast.Call, Dict[str, ast.AST]]] = []
        self.decorated_dags: List[Tuple[ast.AST, Dict[str, ast.AST]]] = []
        self.taskflow_tasks: List[Tuple[ast.AST, Dict[str, ast.AST]]] = []
        self.operator_calls: List[Tuple[ast.Call, str, Dict[str, ast.AST], bool]] = []
        self.named_dicts: Dict[str, ast.Dict] = {}
        self.assigned_var: Dict[int, str] = {}
        self.taskgroup_seen = False
        self.doc_md_assigned = False
        self.xcom_calls: List[Tuple[str, ast.Call, Optional[str]]] = []
        self.template_pulls: List[Tuple[int, Optional[str]]] = []
        self.fn_returns: Dict[str, List[ast.expr]] = {}
        self.chain_edges: List[Tuple[str, str]] = []
        self.dynamic_loops: List[Tuple[ast.AST, List[Tuple[str, int]]]] = []
        self._fn_stack: List[str] = []
        self._loop_stack: List[List[Tuple[str, int]]] = []
        self._loop_nodes: List[ast.AST] = []
        self._binop_seen: Set[int] = set()

    # -- statements ---------------------------------------------------------

    def visit_Assign(self, node: ast.Assign) -> None:
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if isinstance(node.value, ast.Dict):
                self.named_dicts[name] = node.value
            if isinstance(node.value, ast.Call):
                self.assigned_var[id(node.value)] = name
        for target in node.targets:
            if isinstance(target, ast.Attribute) and target.attr == "doc_md":
                self.doc_md_assigned = True
        self.generic_visit(node)

    def _visit_function(self, node: ast.AST) -> None:
        for dec in getattr(node, "decorator_list", []):
            dec_call = dec if isinstance(dec, ast.Call) else None
            dec_target = dec_call.func if dec_call else dec
            name = _terminal_name(dec_target)
            if name == "dag":
                self.decorated_dags.append((node, _kwargs_map(dec_call)))
            elif name == "task" or _root_name(dec_target) == "task":
                self.taskflow_tasks.append((node, _kwargs_map(dec_call)))
        self._fn_stack.append(getattr(node, "name", "<lambda>"))
        self.generic_visit(node)
        self._fn_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Return(self, node: ast.Return) -> None:
        if (
            self._fn_stack
            and node.value is not None
            and not (isinstance(node.value, ast.Constant) and node.value.value is None)
        ):
            self.fn_returns.setdefault(self._fn_stack[-1], []).append(node.value)
        self.generic_visit(node)

    def _visit_loop(self, node: ast.AST) -> None:
        constructs: List[Tuple[str, int]] = []
        self._loop_stack.append(constructs)
        self._loop_nodes.append(node)
        self.generic_visit(node)
        self._loop_nodes.pop()
        self._loop_stack.pop()
        if constructs:
            self.dynamic_loops.append((node, constructs))

    def visit_For(self, node: ast.For) -> None:
        self._visit_loop(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self._visit_loop(node)

    def visit_While(self, node: ast.While) -> None:
        self._visit_loop(node)

    # -- expressions --------------------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:
        cname = _terminal_name(node.func)
        kwargs = _kwargs_map(node)
        if cname == "DAG":
            self.dag_calls.append((node, kwargs))
            self._mark_dynamic("DAG", node)
        elif cname == "TaskGroup":
            self.taskgroup_seen = True
        elif cname.endswith("Sensor"):
            self.operator_calls.append((node, cname, kwargs, True))
            self._mark_dynamic(cname, node)
        elif cname.endswith("Operator"):
            self.operator_calls.append((node, cname, kwargs, False))
            self._mark_dynamic(cname, node)
        elif cname == "chain":
            self._handle_chain_helper(node)
        if isinstance(node.func, ast.Attribute):
            attr = node.func.attr
            enclosing = self._fn_stack[-1] if self._fn_stack else None
            if attr == "xcom_push":
                self.xcom_calls.append(("push", node, enclosing))
            elif attr == "xcom_pull":
                self.xcom_calls.append(("pull", node, enclosing))
            elif attr == "set_downstream":
                self._handle_set_dep(node, downstream=True)
            elif attr == "set_upstream":
                self._handle_set_dep(node, downstream=False)
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and "xcom_pull" in node.value:
            match = _TEMPLATE_PULL_RE.search(node.value)
            task_id = match.group(1) if match else None
            self.template_pulls.append((int(getattr(node, "lineno", 0) or 0), task_id))
        self.generic_visit(node)

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if isinstance(node.op, (ast.RShift, ast.LShift)) and id(node) not in self._binop_seen:
            self._eval_shift_chain(node)
        self.generic_visit(node)

    # -- helpers ------------------------------------------------------------

    def _mark_dynamic(self, construct: str, node: ast.AST) -> None:
        if self._loop_stack:
            self._loop_stack[-1].append((construct, int(getattr(node, "lineno", 0) or 0)))

    def _names_of(self, node: ast.AST) -> List[str]:
        if isinstance(node, ast.Name):
            return [node.id]
        if isinstance(node, (ast.List, ast.Tuple)):
            return [e.id for e in node.elts if isinstance(e, ast.Name)]
        return []

    def _eval_shift_chain(self, node: ast.AST) -> List[str]:
        """Evaluate ``>>``/``<<`` chains, recording edges; returns the rhs operand."""
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.RShift, ast.LShift)):
            self._binop_seen.add(id(node))
            left = self._eval_shift_chain(node.left)
            right = self._eval_shift_chain(node.right)
            if isinstance(node.op, ast.RShift):
                self.chain_edges.extend((l, r) for l in left for r in right)
            else:
                self.chain_edges.extend((r, l) for l in left for r in right)
            return right
        return self._names_of(node)

    def _handle_set_dep(self, node: ast.Call, *, downstream: bool) -> None:
        if not isinstance(node.func, ast.Attribute):
            return
        base = self._names_of(node.func.value)
        others: List[str] = []
        for arg in node.args:
            others.extend(self._names_of(arg))
        for b in base:
            for o in others:
                self.chain_edges.append((b, o) if downstream else (o, b))

    def _handle_chain_helper(self, node: ast.Call) -> None:
        """``chain(a, b, c)`` -> sequential edges a->b->c (lists fan in/out)."""
        groups = [self._names_of(arg) for arg in node.args]
        groups = [g for g in groups if g]
        for prev, nxt in zip(groups, groups[1:]):
            self.chain_edges.extend((p, n) for p in prev for n in nxt)


def _resolve_default_args(
    kwargs: Dict[str, ast.AST], named_dicts: Dict[str, ast.Dict]
) -> Dict[str, ast.AST]:
    """Resolve ``default_args`` (inline dict or module-level variable) to key->node."""
    node: Optional[ast.AST] = kwargs.get("default_args")
    if isinstance(node, ast.Name):
        node = named_dicts.get(node.id)
    if not isinstance(node, ast.Dict):
        return {}
    out: Dict[str, ast.AST] = {}
    for key, value in zip(node.keys, node.values):
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            out[key.value] = value
    return out


def _build_dag_details(
    kwargs: Dict[str, ast.AST],
    defaults: Dict[str, ast.AST],
    *,
    dag_id: Optional[str],
    task_count: int,
    has_task_groups: bool,
    doc_fallback: bool,
) -> Tuple[Dict[str, Any], Optional[str], Optional[int]]:
    """DAG details dict plus the derived ``(cron, interval_minutes)`` pair."""
    schedule_node = kwargs["schedule"] if "schedule" in kwargs else kwargs.get("schedule_interval")
    schedule_str, cron, interval = _schedule_info(schedule_node)
    details: Dict[str, Any] = {
        "dag_id": dag_id,
        "schedule": schedule_str,
        "catchup": _explicit_bool(kwargs["catchup"]) if "catchup" in kwargs else None,
        "task_count": task_count,
        "has_task_groups": has_task_groups,
        "tags": _str_list(kwargs.get("tags")),
        "has_dagrun_timeout": "dagrun_timeout" in kwargs,
        "max_active_runs": _int_literal(kwargs.get("max_active_runs")),
        "has_doc_md": "doc_md" in kwargs or doc_fallback,
        "default_retries": _int_literal(defaults.get("retries")),
        "has_retry_delay": "retry_delay" in defaults,
        "owner": _str_literal(defaults.get("owner")),
        "has_sla": "sla" in defaults or "sla" in kwargs,
        "has_on_failure_callback": (
            "on_failure_callback" in kwargs or "on_failure_callback" in defaults
        ),
        "depends_on_past": (
            _explicit_bool(defaults["depends_on_past"])
            if "depends_on_past" in defaults
            else False
        ),
    }
    return details, cron, interval


def _trigger_rule_value(node: Optional[ast.AST]) -> str:
    """Normalize a ``trigger_rule`` kwarg (string or TriggerRule.X attribute)."""
    text = _str_literal(node)
    if text:
        return text
    if isinstance(node, ast.Attribute):
        return node.attr.lower()
    return "all_success"


def parse_airflow(source: str) -> ParseResult:
    """Parse an Airflow DAG file into the unified IR.

    Emits one ``DAG`` operation per ``DAG(...)`` constructor / ``with DAG`` /
    ``@dag`` decorator (merging ``default_args``), ``TASK``/``SENSOR``
    operations per operator instantiation, ``XCOM`` operations,
    ``DYNAMIC_DAG`` markers and ``triggers`` task-to-task dependencies.

    Raises:
        ParseError: when the source is not valid Python (includes line number).
    """
    module = _parse_python_module(source, "Airflow DAG")
    collector = _AirflowCollector()
    collector.visit(module)

    try:
        warnings: List[str] = []
        named_dicts = collector.named_dicts

        # DAG records (constructor / context-manager form + @dag decorator form).
        dag_records: List[Dict[str, Any]] = []
        for call, kwargs in collector.dag_calls:
            dag_id = _str_literal(kwargs.get("dag_id"))
            if dag_id is None and call.args:
                dag_id = _str_literal(call.args[0])
            dag_records.append(
                {
                    "node": call,
                    "kwargs": kwargs,
                    "dag_id": dag_id,
                    "doc_fallback": collector.doc_md_assigned,
                }
            )
        for fn, kwargs in collector.decorated_dags:
            dag_records.append(
                {
                    "node": fn,
                    "kwargs": kwargs,
                    "dag_id": _str_literal(kwargs.get("dag_id")) or getattr(fn, "name", None),
                    "doc_fallback": bool(ast.get_docstring(fn))
                    or collector.doc_md_assigned,
                }
            )

        merged_defaults: Dict[str, ast.AST] = {}
        for rec in dag_records:
            for key, value in _resolve_default_args(rec["kwargs"], named_dicts).items():
                merged_defaults.setdefault(key, value)

        # Tasks & sensors.
        task_ops: List[Operation] = []
        sensor_ops: List[Operation] = []
        var_to_task: Dict[str, str] = {}
        fn_to_task: Dict[str, str] = {}
        for call, cname, kwargs, is_sensor in collector.operator_calls:
            var = collector.assigned_var.get(id(call))
            task_id = _str_literal(kwargs.get("task_id")) or var
            if task_id is None and isinstance(kwargs.get("task_id"), ast.JoinedStr):
                # Dynamic (f-string) task ids cannot be resolved statically;
                # keep the template text so rule messages stay meaningful.
                try:
                    task_id = ast.unparse(kwargs["task_id"])
                except Exception:  # noqa: BLE001 - display-only best effort
                    task_id = None
            if var and task_id:
                var_to_task[var] = task_id
            callable_node = kwargs.get("python_callable")
            if isinstance(callable_node, ast.Name) and task_id:
                fn_to_task[callable_node.id] = task_id
            if is_sensor:
                sensor_ops.append(
                    Operation(
                        type="SENSOR",
                        location=_location(call),
                        details={
                            "task_id": task_id,
                            "operator": cname,
                            "mode": _str_literal(kwargs.get("mode")) or "poke",
                            "poke_interval": _seconds_value(kwargs.get("poke_interval")),
                            "timeout": _seconds_value(kwargs.get("timeout")),
                        },
                    )
                )
                continue
            retries = _int_literal(kwargs.get("retries"))
            if retries is None:
                retries = _int_literal(merged_defaults.get("retries"))
            task_ops.append(
                Operation(
                    type="TASK",
                    location=_location(call),
                    details={
                        "task_id": task_id,
                        "operator": cname,
                        "retries": retries,
                        "has_retry_delay": "retry_delay" in kwargs
                        or "retry_delay" in merged_defaults,
                        "pool": _str_literal(kwargs.get("pool")),
                        "has_sla": "sla" in kwargs or "sla" in merged_defaults,
                        "owner": _str_literal(kwargs.get("owner"))
                        or _str_literal(merged_defaults.get("owner")),
                        "has_on_failure_callback": "on_failure_callback" in kwargs
                        or "on_failure_callback" in merged_defaults,
                        "has_execution_timeout": "execution_timeout" in kwargs
                        or "execution_timeout" in merged_defaults,
                        "trigger_rule": _trigger_rule_value(kwargs.get("trigger_rule")),
                        "is_heavy": bool(_HEAVY_OPERATOR_RE.search(cname)),
                    },
                )
            )

        # TaskFlow (@task) functions are PythonOperator-backed tasks.
        taskflow_names: Set[str] = set()
        for fn, dkw in collector.taskflow_tasks:
            fn_name = getattr(fn, "name", None) or "<task>"
            task_id = _str_literal(dkw.get("task_id")) or fn_name
            taskflow_names.add(fn_name)
            fn_to_task[fn_name] = task_id
            retries = _int_literal(dkw.get("retries"))
            if retries is None:
                retries = _int_literal(merged_defaults.get("retries"))
            task_ops.append(
                Operation(
                    type="TASK",
                    location=_location(fn),
                    details={
                        "task_id": task_id,
                        "operator": "PythonOperator",
                        "retries": retries,
                        "has_retry_delay": "retry_delay" in dkw
                        or "retry_delay" in merged_defaults,
                        "pool": _str_literal(dkw.get("pool")),
                        "has_sla": "sla" in dkw or "sla" in merged_defaults,
                        "owner": _str_literal(dkw.get("owner"))
                        or _str_literal(merged_defaults.get("owner")),
                        "has_on_failure_callback": "on_failure_callback" in dkw
                        or "on_failure_callback" in merged_defaults,
                        "has_execution_timeout": "execution_timeout" in dkw
                        or "execution_timeout" in merged_defaults,
                        "trigger_rule": _trigger_rule_value(dkw.get("trigger_rule")),
                        "is_heavy": bool(_HEAVY_NAME_RE.search(fn_name)),
                    },
                )
            )

        # XCom operations.
        xcom_ops: List[Operation] = []
        for kind, call, enclosing_fn in collector.xcom_calls:
            kwargs = _kwargs_map(call)
            if kind == "push":
                value_node = kwargs.get("value")
                if value_node is None and len(call.args) >= 2:
                    value_node = call.args[1]
                details: Dict[str, Any] = {
                    "kind": "push",
                    "task_id": fn_to_task.get(enclosing_fn or ""),
                    "large_data_hint": _large_data_hint(value_node),
                }
            else:
                pulled = _str_literal(kwargs.get("task_ids"))
                if pulled is None:
                    ids = _str_list(kwargs.get("task_ids"))
                    pulled = ids[0] if ids else None
                details = {
                    "kind": "pull",
                    "task_id": pulled or fn_to_task.get(enclosing_fn or ""),
                    "large_data_hint": False,
                }
            xcom_ops.append(Operation(type="XCOM", location=_location(call), details=details))
        for line, pulled in collector.template_pulls:
            xcom_ops.append(
                Operation(
                    type="XCOM",
                    location=Location(line=line),
                    details={"kind": "pull", "task_id": pulled, "large_data_hint": False},
                )
            )
        for fn_name, return_exprs in collector.fn_returns.items():
            if fn_name not in fn_to_task:
                continue
            xcom_ops.append(
                Operation(
                    type="XCOM",
                    location=_location(return_exprs[0]),
                    details={
                        "kind": "return_value",
                        "task_id": fn_to_task[fn_name],
                        "large_data_hint": any(_large_data_hint(e) for e in return_exprs),
                    },
                )
            )

        # DYNAMIC_DAG markers.
        dynamic_ops: List[Operation] = []
        for loop_node, constructs in collector.dynamic_loops:
            names = [name for name, _ in constructs]
            dynamic_ops.append(
                Operation(
                    type="DYNAMIC_DAG",
                    location=_location(loop_node),
                    details={
                        "construct": names[0],
                        "constructs": sorted(set(names)),
                        "count": len(constructs),
                        "name": f"{names[0]} instances",
                    },
                )
            )

        # Dependencies (>> / << chains, set_upstream/downstream, chain(), TaskFlow).
        dependencies: List[Dependency] = []
        seen_edges: Set[Tuple[str, str]] = set()

        def add_edge(src: str, dst: str) -> None:
            source_id = var_to_task.get(src, src)
            target_id = var_to_task.get(dst, dst)
            if not source_id or not target_id or source_id == target_id:
                return
            key = (source_id, target_id)
            if key in seen_edges:
                return
            seen_edges.add(key)
            dependencies.append(
                Dependency(source=source_id, target=target_id, type="triggers")
            )

        for src, dst in collector.chain_edges:
            add_edge(src, dst)
        if taskflow_names:
            for fn, _kw in collector.decorated_dags:
                graph_edges, _ = _call_graph_edges(fn, taskflow_names)
                for src, dst in graph_edges:
                    add_edge(fn_to_task.get(src, src), fn_to_task.get(dst, dst))
            # TaskFlow tasks invoked at module level (e.g. inside a
            # ``with DAG(...):`` block) also form a dataflow graph.
            graph_edges, _ = _call_graph_edges(module, taskflow_names)
            for src, dst in graph_edges:
                add_edge(fn_to_task.get(src, src), fn_to_task.get(dst, dst))

        # DAG operations + scheduling.
        task_count = len(task_ops) + len(sensor_ops)
        dag_ops: List[Operation] = []
        scheduling = Scheduling()
        metadata = IRMetadata()
        for index, rec in enumerate(dag_records):
            defaults = _resolve_default_args(rec["kwargs"], named_dicts)
            details, cron, interval = _build_dag_details(
                rec["kwargs"],
                defaults,
                dag_id=rec["dag_id"],
                task_count=task_count,
                has_task_groups=collector.taskgroup_seen,
                doc_fallback=rec["doc_fallback"],
            )
            dag_ops.append(
                Operation(type="DAG", location=_location(rec["node"]), details=details)
            )
            if index == 0:
                sla_minutes = _timedelta_minutes(defaults.get("sla"))
                scheduling = Scheduling(
                    cron=cron,
                    interval_minutes=interval,
                    sla_minutes=int(round(sla_minutes)) if sla_minutes else None,
                    catchup=details["catchup"],
                    retries=details["default_retries"],
                )
                metadata = IRMetadata(name=rec["dag_id"], tags=details["tags"])

        if not dag_records:
            warnings.append(
                "No DAG(...) constructor or @dag decorator found; emitted "
                "task-level IR only."
            )

        ir = IR(
            format="airflow",
            tables=[],
            operations=dag_ops + task_ops + sensor_ops + xcom_ops + dynamic_ops,
            dependencies=dependencies,
            scheduling=scheduling,
            metadata=metadata,
        )
        logger.debug(
            "parse_airflow: %d DAG(s), %d task(s), %d sensor(s), %d edge(s)",
            len(dag_ops),
            len(task_ops),
            len(sensor_ops),
            len(dependencies),
        )
        return ParseResult(ir=ir, source=source, ast=module, warnings=warnings)
    except ParseError:
        raise
    except Exception as exc:  # noqa: BLE001 - malformed-but-valid Python must not crash
        logger.exception("parse_airflow failed on structurally unusual input")
        raise ParseError(f"Failed to analyze Airflow DAG source: {exc}") from exc


# ---------------------------------------------------------------------------
# Prefect / Dagster
# ---------------------------------------------------------------------------

_FLOW_DECORATORS = {"flow": "prefect.flow", "job": "dagster.job"}
_TASK_DECORATORS = {"task": "prefect.task", "op": "dagster.op", "asset": "dagster.asset"}


def _decorator_retry_info(kwargs: Dict[str, ast.AST]) -> Tuple[Optional[int], bool]:
    """``(retries, has_retry_delay)`` from Prefect kwargs or a Dagster RetryPolicy."""
    retries = _int_literal(kwargs.get("retries"))
    has_delay = "retry_delay_seconds" in kwargs
    policy = kwargs.get("retry_policy")
    if isinstance(policy, ast.Call):
        policy_kwargs = _kwargs_map(policy)
        if retries is None:
            retries = _int_literal(policy_kwargs.get("max_retries"))
        has_delay = has_delay or "delay" in policy_kwargs
    return retries, has_delay


def parse_prefect(source: str) -> ParseResult:
    """Parse a Prefect (``@flow``/``@task``) or Dagster (``@op``/``@job``/``@asset``)
    pipeline into the unified IR.

    Flows/jobs become ``DAG`` operations (Airflow-only detail keys are null),
    tasks/ops/assets become ``TASK`` operations, and dataflow inside the
    flow/job body (task A's result passed into task B) plus Dagster asset
    inputs become ``triggers`` dependency edges.

    Raises:
        ParseError: when the source is not valid Python (includes line number).
    """
    module = _parse_python_module(source, "Prefect/Dagster flow")
    try:
        warnings: List[str] = []
        flows: List[Tuple[ast.AST, str, Dict[str, ast.AST]]] = []
        task_recs: Dict[str, Tuple[ast.AST, str, Dict[str, ast.AST]]] = {}

        for node in ast.walk(module):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                dec_call = dec if isinstance(dec, ast.Call) else None
                name = _terminal_name(dec_call.func if dec_call else dec)
                kwargs = _kwargs_map(dec_call)
                if name in _FLOW_DECORATORS:
                    flows.append((node, _FLOW_DECORATORS[name], kwargs))
                elif name in _TASK_DECORATORS:
                    task_recs[node.name] = (node, _TASK_DECORATORS[name], kwargs)

        task_ops: List[Operation] = []
        for task_name, (fn, operator, kwargs) in task_recs.items():
            retries, has_delay = _decorator_retry_info(kwargs)
            task_ops.append(
                Operation(
                    type="TASK",
                    location=_location(fn),
                    details={
                        "task_id": task_name,
                        "operator": operator,
                        "retries": retries,
                        "has_retry_delay": has_delay,
                        "pool": None,
                        "has_sla": None,
                        "owner": None,
                        "has_on_failure_callback": "on_failure" in kwargs,
                        "has_execution_timeout": "timeout_seconds" in kwargs,
                        "trigger_rule": None,
                        "is_heavy": bool(_HEAVY_NAME_RE.search(task_name)),
                    },
                )
            )

        # Dependencies: dataflow inside flow/job bodies + Dagster asset inputs.
        task_names = set(task_recs)
        dependencies: List[Dependency] = []
        seen_edges: Set[Tuple[str, str]] = set()

        def add_edge(src: str, dst: str) -> None:
            if not src or not dst or src == dst or (src, dst) in seen_edges:
                return
            seen_edges.add((src, dst))
            dependencies.append(Dependency(source=src, target=dst, type="triggers"))

        dag_ops: List[Operation] = []
        for fn, kind, kwargs in flows:
            graph_edges, invoked = _call_graph_edges(fn, task_names)
            for src, dst in graph_edges:
                add_edge(src, dst)
            retries, has_delay = _decorator_retry_info(kwargs)
            dag_ops.append(
                Operation(
                    type="DAG",
                    location=_location(fn),
                    details={
                        "dag_id": getattr(fn, "name", None),
                        "schedule": None,
                        "catchup": None,
                        "task_count": len(invoked) if invoked else len(task_ops),
                        "has_task_groups": None,
                        "tags": _str_list(kwargs.get("tags")),
                        "has_dagrun_timeout": None,
                        "max_active_runs": None,
                        "has_doc_md": bool(ast.get_docstring(fn)),
                        "default_retries": retries,
                        "has_retry_delay": has_delay,
                        "owner": None,
                        "has_sla": None,
                        "has_on_failure_callback": "on_failure" in kwargs,
                        "depends_on_past": None,
                        "kind": kind,
                    },
                )
            )

        for task_name, (fn, operator, kwargs) in task_recs.items():
            if operator != "dagster.asset":
                continue
            args = getattr(fn, "args", None)
            params = list(getattr(args, "posonlyargs", [])) + list(getattr(args, "args", []))
            params += list(getattr(args, "kwonlyargs", []))
            for param in params:
                if param.arg != "context" and param.arg in task_names:
                    add_edge(param.arg, task_name)
            deps_node = kwargs.get("deps")
            if isinstance(deps_node, (ast.List, ast.Tuple)):
                for elt in deps_node.elts:
                    upstream = _str_literal(elt) or (
                        elt.id if isinstance(elt, ast.Name) else None
                    )
                    if upstream:
                        add_edge(upstream, task_name)

        if not flows and not task_recs:
            warnings.append(
                "No @flow/@task (Prefect) or @op/@job/@asset (Dagster) "
                "decorators found; the IR is empty."
            )

        first_dag = dag_ops[0].details if dag_ops else {}
        scheduling = Scheduling(retries=first_dag.get("default_retries"))
        metadata = IRMetadata(
            name=first_dag.get("dag_id"), tags=list(first_dag.get("tags") or [])
        )
        ir = IR(
            format="prefect",
            tables=[],
            operations=dag_ops + task_ops,
            dependencies=dependencies,
            scheduling=scheduling,
            metadata=metadata,
        )
        logger.debug(
            "parse_prefect: %d flow/job(s), %d task(s), %d edge(s)",
            len(dag_ops),
            len(task_ops),
            len(dependencies),
        )
        return ParseResult(ir=ir, source=source, ast=module, warnings=warnings)
    except ParseError:
        raise
    except Exception as exc:  # noqa: BLE001 - malformed-but-valid Python must not crash
        logger.exception("parse_prefect failed on structurally unusual input")
        raise ParseError(f"Failed to analyze Prefect/Dagster source: {exc}") from exc
