"""Airflow and Prefect/Dagster orchestration rules.

Twenty deterministic checks over the orchestrator IR produced by
``app.parsers.airflow_parser``. That parser emits ``DAG``, ``TASK``,
``SENSOR``, ``XCOM`` and ``DYNAMIC_DAG`` operations whose ``details`` dicts
carry the structured facts documented in :class:`app.schemas.ir.Operation`.
Every rule reads those details defensively with ``.get`` so a partially
populated IR (or the Prefect/Dagster parser, which shares this IR shape)
never crashes analysis.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, Dict, FrozenSet, List, Optional

from app.rules import Rule, register
from app.schemas.ir import Operation, ParseResult
from app.schemas.report import Issue

logger = logging.getLogger(__name__)

AIRFLOW_ONLY: FrozenSet[str] = frozenset({"airflow"})
ORCHESTRATORS: FrozenSet[str] = frozenset({"airflow", "prefect"})

#: A poke-mode sensor sleeping this long (seconds) or longer wastes a worker slot.
LONG_POKE_INTERVAL_SECONDS = 60
#: DAGs with at least this many tasks should organize them into TaskGroups.
TASK_GROUP_THRESHOLD = 12


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _details(op: Operation) -> Dict[str, Any]:
    """Return the operation's details dict, never ``None``."""
    return op.details or {}


def _line(op: Operation) -> Optional[int]:
    """Return the operation's 1-based line, or ``None`` when unknown."""
    line = op.location.line if op.location else 0
    return line if line and line > 0 else None


def _dag_ops(pr: ParseResult) -> List[Operation]:
    """All DAG (or flow) operations in the parse result."""
    return pr.ir.ops("DAG")


def _task_ops(pr: ParseResult) -> List[Operation]:
    """All task operations in the parse result."""
    return pr.ir.ops("TASK")


def _sensor_ops(pr: ParseResult) -> List[Operation]:
    """All sensor operations in the parse result."""
    return pr.ir.ops("SENSOR")


def _dag_id(op: Operation) -> str:
    """Human-readable DAG identifier for messages."""
    return str(_details(op).get("dag_id") or "the DAG")


def _task_id(op: Operation) -> str:
    """Human-readable task identifier for messages."""
    return str(_details(op).get("task_id") or "<unnamed task>")


def _as_int(value: Any) -> Optional[int]:
    """Best-effort int coercion; ``None`` for missing/garbage values."""
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> Optional[float]:
    """Best-effort float coercion; ``None`` for missing/garbage values."""
    if isinstance(value, bool):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_dag(pr: ParseResult) -> Optional[Operation]:
    """The first DAG operation, used for default_args-style inheritance."""
    dags = _dag_ops(pr)
    return dags[0] if dags else None


def _dag_default_retries(pr: ParseResult) -> Optional[int]:
    """DAG-level ``default_args['retries']`` if the parser captured one."""
    dag = _first_dag(pr)
    return _as_int(_details(dag).get("default_retries")) if dag else None


def _dag_has_retry_delay(pr: ParseResult) -> bool:
    """True when the DAG's default_args define a retry_delay."""
    dag = _first_dag(pr)
    return bool(_details(dag).get("has_retry_delay")) if dag else False


def _linear_chains(pr: ParseResult) -> List[List[str]]:
    """Maximal straight task chains (no fan-in/fan-out between links).

    Uses ``triggers`` dependency edges. An edge ``u -> v`` is part of a
    straight chain only when ``u`` has exactly one successor and ``v`` has
    exactly one predecessor - i.e. nothing between them could already be
    running in parallel.
    """
    succ: Dict[str, set] = defaultdict(set)
    pred: Dict[str, set] = defaultdict(set)
    for dep in pr.ir.dependencies:
        if dep.type != "triggers" or not dep.source or not dep.target:
            continue
        if dep.source == dep.target:
            continue
        succ[dep.source].add(dep.target)
        pred[dep.target].add(dep.source)

    nxt: Dict[str, str] = {}
    for upstream, targets in succ.items():
        if len(targets) != 1:
            continue
        (downstream,) = tuple(targets)
        if len(pred[downstream]) == 1:
            nxt[upstream] = downstream

    has_linear_pred = set(nxt.values())
    chains: List[List[str]] = []
    for start in nxt:
        if start in has_linear_pred:
            continue
        chain = [start]
        seen = {start}
        cursor = start
        while cursor in nxt:
            cursor = nxt[cursor]
            if cursor in seen:  # cycle guard - malformed graphs must not hang
                break
            chain.append(cursor)
            seen.add(cursor)
        if len(chain) >= 2:
            chains.append(chain)
    return chains


# ---------------------------------------------------------------------------
# CRITICAL rules
# ---------------------------------------------------------------------------

@register
class NoRetriesRule(Rule):
    """Tasks whose effective retry count is zero fail on any transient error."""

    id = "NO_RETRIES"
    severity = "CRITICAL"
    category = "reliability"
    formats = ORCHESTRATORS
    title = "Tasks run with zero retries"
    description = (
        "Tasks without retries (no task-level retries and no DAG default_args "
        "retries) fail permanently on any transient error such as a network "
        "blip, API rate limit or warehouse queue timeout."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag tasks (and bare DAGs) whose effective retries are 0/None."""
        dag = _first_dag(pr)
        dag_retries = _dag_default_retries(pr)
        tasks = _task_ops(pr)

        unprotected = []
        for task in tasks:
            own = _as_int(_details(task).get("retries"))
            effective = own if own is not None else dag_retries
            if not effective:
                unprotected.append(task)

        dag_label = _dag_id(dag) if dag else "the pipeline"
        owner = str(_details(dag).get("owner") or "data-eng") if dag else "data-eng"
        fix_diff = (
            "--- current\n"
            "+++ optimized\n"
            f'-default_args = {{"owner": "{owner}"}}\n'
            "+default_args = {\n"
            f'+    "owner": "{owner}",\n'
            '+    "retries": 3,\n'
            '+    "retry_delay": timedelta(minutes=5),\n'
            "+}"
        )
        fix_suggestion = (
            "Set retries (e.g. retries=3 with retry_delay=timedelta(minutes=5)) "
            "in the DAG default_args so every task inherits it, then override "
            "per task only where retrying is unsafe."
        )

        if tasks and unprotected:
            names = ", ".join(f"'{_task_id(t)}'" for t in unprotected[:8])
            extra = (
                f" (+{len(unprotected) - 8} more)" if len(unprotected) > 8 else ""
            )
            message = (
                f"{len(unprotected)} of {len(tasks)} task(s) in {dag_label} have "
                f"an effective retry count of 0: {names}{extra}. A single "
                "transient failure permanently fails the run."
            )
            return [
                self.issue(
                    message,
                    line=_line(unprotected[0]),
                    fix_suggestion=fix_suggestion,
                    fix_diff=fix_diff,
                )
            ]
        if not tasks and dag is not None and not dag_retries:
            message = (
                f"DAG {dag_label!r} defines no default retries "
                "(default_args['retries'] is 0 or unset), so its tasks will "
                "fail permanently on the first transient error."
            )
            return [
                self.issue(
                    message,
                    line=_line(dag),
                    fix_suggestion=fix_suggestion,
                    fix_diff=fix_diff,
                )
            ]
        return []


@register
class NoCatchupSetRule(Rule):
    """Unset catchup means Airflow backfills every missed interval by default."""

    id = "NO_CATCHUP_SET"
    severity = "CRITICAL"
    category = "reliability"
    formats = AIRFLOW_ONLY
    title = "catchup not set explicitly"
    description = (
        "When catchup is not set, Airflow defaults to catchup=True and will "
        "schedule a run for every interval since start_date the moment the DAG "
        "is deployed - an accidental, often expensive backfill storm."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag DAGs whose ``catchup`` was never set (null, not explicit False)."""
        issues: List[Issue] = []
        for dag in _dag_ops(pr):
            details = _details(dag)
            if details.get("catchup") is not None:
                continue
            dag_id = _dag_id(dag)
            schedule = details.get("schedule")
            schedule_line = (
                f'     schedule="{schedule}",\n' if isinstance(schedule, str) else ""
            )
            fix_diff = (
                "--- current\n"
                "+++ optimized\n"
                " with DAG(\n"
                f'     dag_id="{dag_id}",\n'
                f"{schedule_line}"
                "+    catchup=False,\n"
                " ) as dag:"
            )
            issues.append(
                self.issue(
                    f"DAG {dag_id!r} does not set catchup. Airflow defaults to "
                    "catchup=True, so the first deploy will backfill every "
                    "missed interval since start_date.",
                    line=_line(dag),
                    fix_suggestion=(
                        "Set catchup explicitly in the DAG constructor - "
                        "catchup=False for most operational DAGs, or "
                        "catchup=True only when historical backfill is intended."
                    ),
                    fix_diff=fix_diff,
                )
            )
        return issues


@register
class SequentialHeavyTasksRule(Rule):
    """Heavy tasks chained strictly one-after-another waste wall-clock time."""

    id = "SEQUENTIAL_HEAVY_TASKS"
    severity = "CRITICAL"
    category = "performance"
    formats = ORCHESTRATORS
    title = "Heavy tasks run sequentially"
    description = (
        "Two or more resource-heavy tasks sit in a straight dependency chain "
        "with no branching between them; if they are independent they could "
        "run in parallel and cut pipeline wall-clock time."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag straight ``triggers`` chains containing 2+ heavy tasks."""
        heavy_by_id: Dict[str, Operation] = {}
        for task in _task_ops(pr):
            details = _details(task)
            task_id = details.get("task_id")
            if task_id and details.get("is_heavy"):
                heavy_by_id[str(task_id)] = task

        if len(heavy_by_id) < 2:
            return []

        issues: List[Issue] = []
        for chain in _linear_chains(pr):
            heavy_in_chain = [t for t in chain if t in heavy_by_id]
            if len(heavy_in_chain) < 2:
                continue
            names = " -> ".join(f"'{t}'" for t in heavy_in_chain)
            first_op = heavy_by_id[heavy_in_chain[0]]
            issues.append(
                self.issue(
                    f"Heavy tasks {names} run strictly one after another in the "
                    f"chain {' -> '.join(chain)}. There is no branching between "
                    "them, so if they do not consume each other's output they "
                    "can be scheduled in parallel.",
                    line=_line(first_op),
                    fix_suggestion=(
                        "Restructure the dependencies so independent heavy tasks "
                        "fan out from a common upstream (e.g. "
                        f"upstream >> [{', '.join(heavy_in_chain)}] >> downstream) "
                        "instead of chaining them sequentially."
                    ),
                )
            )
        return issues


@register
class XcomsLargeDataRule(Rule):
    """XCom is metadata plumbing, not a data plane."""

    id = "XCOMS_LARGE_DATA"
    severity = "CRITICAL"
    category = "reliability"
    formats = AIRFLOW_ONLY
    title = "Large data passed through XCom"
    description = (
        "XCom values are serialized into the Airflow metadata database; "
        "pushing DataFrames or query results bloats the DB, slows the "
        "scheduler and fails outright above the size limit."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag XCom pushes/pulls the parser marked as likely-large payloads."""
        issues: List[Issue] = []
        for op in pr.ir.ops("XCOM", "XCOM_PUSH", "XCOM_PULL"):
            details = _details(op)
            if not details.get("large_data_hint"):
                continue
            task_id = str(details.get("task_id") or "<unknown task>")
            kind = str(details.get("kind") or "xcom")
            issues.append(
                self.issue(
                    f"Task {task_id!r} moves what looks like a large payload "
                    f"through XCom ({kind}). XCom is stored in the Airflow "
                    "metadata database and breaks for anything bigger than a "
                    "few kilobytes.",
                    line=_line(op),
                    fix_suggestion=(
                        "Write the dataset to object storage or a staging table "
                        "inside the task and pass only the URI/table name "
                        "through XCom (or configure a custom XCom backend such "
                        "as S3/GCS)."
                    ),
                )
            )
        return issues


# ---------------------------------------------------------------------------
# WARNING rules
# ---------------------------------------------------------------------------

@register
class NoSlaRule(Rule):
    """Without an SLA nobody is told when the pipeline runs late."""

    id = "NO_SLA"
    severity = "WARNING"
    category = "observability"
    formats = AIRFLOW_ONLY
    title = "No SLA configured"
    description = (
        "Neither the DAG default_args nor any task define an SLA, so late or "
        "hung runs produce no alert until a consumer notices stale data."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag DAGs where no SLA exists at DAG or task level."""
        dags = _dag_ops(pr)
        if not dags:
            return []
        if any(_details(t).get("has_sla") for t in _task_ops(pr)):
            return []
        issues: List[Issue] = []
        for dag in dags:
            if _details(dag).get("has_sla"):
                continue
            issues.append(
                self.issue(
                    f"DAG {_dag_id(dag)!r} has no SLA on the DAG or any of its "
                    "tasks - late runs will not trigger any alert.",
                    line=_line(dag),
                    fix_suggestion=(
                        "Add sla=timedelta(...) to default_args (or to the "
                        "critical tasks) and wire sla_miss_callback to your "
                        "alerting channel."
                    ),
                )
            )
        return issues


@register
class NoOnFailureCallbackRule(Rule):
    """Failures should page somebody, not just turn a square red."""

    id = "NO_ON_FAILURE_CALLBACK"
    severity = "WARNING"
    category = "observability"
    formats = ORCHESTRATORS
    title = "No on_failure_callback configured"
    description = (
        "No failure callback is defined at DAG or task level, so failures are "
        "only visible to whoever happens to open the UI."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag DAGs with no failure callback anywhere."""
        dags = _dag_ops(pr)
        if not dags:
            return []
        if any(_details(t).get("has_on_failure_callback") for t in _task_ops(pr)):
            return []
        issues: List[Issue] = []
        for dag in dags:
            if _details(dag).get("has_on_failure_callback"):
                continue
            issues.append(
                self.issue(
                    f"DAG {_dag_id(dag)!r} defines no on_failure_callback on "
                    "the DAG or any task - failed runs will not notify anyone.",
                    line=_line(dag),
                    fix_suggestion=(
                        "Set on_failure_callback in default_args (e.g. a "
                        "Slack/PagerDuty notifier) so every task failure is "
                        "routed to an owner."
                    ),
                )
            )
        return issues


@register
class SensorPokeModeRule(Rule):
    """Poke-mode sensors occupy a worker slot for their whole wait."""

    id = "SENSOR_POKE_MODE"
    severity = "WARNING"
    category = "performance"
    formats = AIRFLOW_ONLY
    title = "Sensor uses poke mode"
    description = (
        "A sensor in poke mode holds a worker slot for its entire wait. With a "
        "long or unset poke_interval this starves the pool; reschedule mode "
        "frees the slot between checks."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag poke-mode sensors with a long or absent poke_interval."""
        issues: List[Issue] = []
        for sensor in _sensor_ops(pr):
            details = _details(sensor)
            mode = str(details.get("mode") or "poke").lower()
            if mode != "poke":
                continue
            interval = _as_float(details.get("poke_interval"))
            if interval is not None and interval < LONG_POKE_INTERVAL_SECONDS:
                continue
            task_id = _task_id(sensor)
            operator = str(details.get("operator") or "Sensor")
            interval_desc = (
                f"poke_interval={interval:g}s"
                if interval is not None
                else "no poke_interval set"
            )
            if interval is None:
                interval_lines = "+    poke_interval=300,\n"
            else:
                interval_lines = f"     poke_interval={interval:g},\n"
            fix_diff = (
                "--- current\n"
                "+++ optimized\n"
                f" {task_id} = {operator}(\n"
                f'     task_id="{task_id}",\n'
                '-    mode="poke",\n'
                '+    mode="reschedule",\n'
                f"{interval_lines}"
                " )"
            )
            issues.append(
                self.issue(
                    f"Sensor {task_id!r} ({operator}) runs in poke mode with "
                    f"{interval_desc}, holding a worker slot for its entire "
                    "wait.",
                    line=_line(sensor),
                    fix_suggestion=(
                        'Switch the sensor to mode="reschedule" so the worker '
                        "slot is released between checks, and set an explicit "
                        "poke_interval (e.g. 300s) plus a timeout."
                    ),
                    fix_diff=fix_diff,
                )
            )
        return issues


@register
class DynamicDagInLoopRule(Rule):
    """DAGs generated in loops are hard to review, diff and debug."""

    id = "DYNAMIC_DAG_IN_LOOP"
    severity = "WARNING"
    category = "maintainability"
    formats = ORCHESTRATORS
    title = "DAGs/tasks generated in a loop"
    description = (
        "DAGs or operators built dynamically inside a loop make the rendered "
        "pipeline invisible in code review and brittle to upstream config "
        "changes."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag each DYNAMIC_DAG marker the parser emitted."""
        issues: List[Issue] = []
        for op in pr.ir.ops("DYNAMIC_DAG"):
            details = _details(op)
            subject = str(
                details.get("dag_id") or details.get("name") or "DAGs/operators"
            )
            issues.append(
                self.issue(
                    f"{subject} are generated inside a loop. The actual DAG "
                    "shape only exists at parse time, which hides it from code "
                    "review and makes failures hard to trace back to source.",
                    line=_line(op),
                    fix_suggestion=(
                        "Prefer a static DAG per pipeline, Airflow Dynamic Task "
                        "Mapping (.expand()) for fan-out within a DAG, or a "
                        "reviewed config file driving a single documented "
                        "factory function."
                    ),
                )
            )
        return issues


@register
class MissingOwnerRule(Rule):
    """Every production pipeline needs a human owner."""

    id = "MISSING_OWNER"
    severity = "WARNING"
    category = "maintainability"
    formats = ORCHESTRATORS
    title = "No owner set"
    description = (
        "No real owner is configured, so when the pipeline breaks at 3am "
        "nobody knows whose pager should ring."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag DAGs whose owner is missing or the 'airflow' default."""
        dags = _dag_ops(pr)
        if not dags:
            return []

        def _has_real_owner(details: Dict[str, Any]) -> bool:
            owner = str(details.get("owner") or "").strip().lower()
            return bool(owner) and owner != "airflow"

        if any(_has_real_owner(_details(t)) for t in _task_ops(pr)):
            return []
        issues: List[Issue] = []
        for dag in dags:
            if _has_real_owner(_details(dag)):
                continue
            issues.append(
                self.issue(
                    f"DAG {_dag_id(dag)!r} has no meaningful owner (missing or "
                    "the 'airflow' default) on the DAG or any task.",
                    line=_line(dag),
                    fix_suggestion=(
                        'Set default_args["owner"] to the owning team or '
                        "on-call alias (e.g. 'data-platform') so alerts and "
                        "audits route correctly."
                    ),
                )
            )
        return issues


@register
class NoPoolSetRule(Rule):
    """Heavy tasks without a pool can saturate shared infrastructure."""

    id = "NO_POOL_SET"
    severity = "WARNING"
    category = "reliability"
    formats = AIRFLOW_ONLY
    title = "Heavy task without a pool"
    description = (
        "Resource-heavy tasks not assigned to an Airflow pool compete in the "
        "default pool and can saturate the warehouse/API when several run at "
        "once."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag each heavy task that has no pool assignment."""
        issues: List[Issue] = []
        for task in _task_ops(pr):
            details = _details(task)
            if not details.get("is_heavy") or details.get("pool"):
                continue
            task_id = _task_id(task)
            operator = str(details.get("operator") or "task")
            issues.append(
                self.issue(
                    f"Heavy task {task_id!r} ({operator}) is not assigned to a "
                    "pool, so concurrent DAG runs can stack unlimited copies of "
                    "it onto the same warehouse or API.",
                    line=_line(task),
                    fix_suggestion=(
                        'Assign the task to a sized pool, e.g. pool="warehouse" '
                        "with a slot count matching what the downstream system "
                        "can absorb."
                    ),
                )
            )
        return issues


@register
class TaskGroupMissingRule(Rule):
    """Big flat DAGs are unreadable in the graph view."""

    id = "TASK_GROUP_MISSING"
    severity = "WARNING"
    category = "maintainability"
    formats = AIRFLOW_ONLY
    title = "Large DAG without TaskGroups"
    description = (
        "A DAG with many tasks and no TaskGroups renders as an unreadable "
        "flat graph and hides the pipeline's logical stages."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag DAGs with 12+ tasks and no TaskGroup usage."""
        task_op_count = len(_task_ops(pr))
        issues: List[Issue] = []
        for dag in _dag_ops(pr):
            details = _details(dag)
            count = _as_int(details.get("task_count")) or task_op_count
            if count < TASK_GROUP_THRESHOLD or details.get("has_task_groups"):
                continue
            issues.append(
                self.issue(
                    f"DAG {_dag_id(dag)!r} has {count} tasks but no TaskGroups; "
                    "the graph view becomes a flat tangle and stages are "
                    "impossible to reason about.",
                    line=_line(dag),
                    fix_suggestion=(
                        "Group related tasks with airflow.utils.task_group."
                        "TaskGroup (e.g. extract/transform/load groups) to make "
                        "structure and reruns manageable."
                    ),
                )
            )
        return issues


@register
class NoDagrunTimeoutRule(Rule):
    """Hung runs without a dagrun_timeout block schedules silently."""

    id = "NO_DAGRUN_TIMEOUT"
    severity = "WARNING"
    category = "reliability"
    formats = AIRFLOW_ONLY
    title = "No dagrun_timeout"
    description = (
        "Without dagrun_timeout, a hung run occupies its DAG-run slot forever "
        "and quietly blocks future scheduled runs."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag DAGs that do not set dagrun_timeout."""
        issues: List[Issue] = []
        for dag in _dag_ops(pr):
            if _details(dag).get("has_dagrun_timeout"):
                continue
            issues.append(
                self.issue(
                    f"DAG {_dag_id(dag)!r} sets no dagrun_timeout - a hung run "
                    "will never be killed and can silently block subsequent "
                    "runs.",
                    line=_line(dag),
                    fix_suggestion=(
                        "Set dagrun_timeout=timedelta(hours=...) on the DAG to "
                        "a value comfortably above the normal runtime so stuck "
                        "runs fail fast and visibly."
                    ),
                )
            )
        return issues


@register
class NoMaxActiveRunsRule(Rule):
    """Unbounded concurrent DAG runs can pile up after downtime."""

    id = "NO_MAX_ACTIVE_RUNS"
    severity = "WARNING"
    category = "reliability"
    formats = AIRFLOW_ONLY
    title = "max_active_runs not set"
    description = (
        "Without max_active_runs, delayed or backfilled schedules launch many "
        "overlapping runs that race each other on the same target tables."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag DAGs whose max_active_runs is unset."""
        issues: List[Issue] = []
        for dag in _dag_ops(pr):
            if _details(dag).get("max_active_runs") is not None:
                continue
            issues.append(
                self.issue(
                    f"DAG {_dag_id(dag)!r} does not set max_active_runs; after "
                    "scheduler downtime or a backfill, overlapping runs can "
                    "race on the same target tables.",
                    line=_line(dag),
                    fix_suggestion=(
                        "Set max_active_runs=1 (or a deliberate small number) "
                        "on the DAG to serialize runs that write to shared "
                        "targets."
                    ),
                )
            )
        return issues


@register
class DependsOnPastRule(Rule):
    """depends_on_past silently serializes and stalls schedules."""

    id = "DEPENDS_ON_PAST"
    severity = "WARNING"
    category = "reliability"
    formats = AIRFLOW_ONLY
    title = "depends_on_past enabled"
    description = (
        "depends_on_past=True makes every run wait for the previous one to "
        "succeed; a single failure silently freezes the whole schedule until "
        "someone intervenes."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag DAGs that enable depends_on_past."""
        issues: List[Issue] = []
        for dag in _dag_ops(pr):
            if not _details(dag).get("depends_on_past"):
                continue
            issues.append(
                self.issue(
                    f"DAG {_dag_id(dag)!r} sets depends_on_past=True. One "
                    "failed run silently blocks every later run until it is "
                    "manually fixed, and runs can never catch up in parallel.",
                    line=_line(dag),
                    fix_suggestion=(
                        "Remove depends_on_past unless strict run ordering is "
                        "a real requirement; if it is, pair it with alerting "
                        "(SLA + on_failure_callback) so a stalled chain is "
                        "noticed immediately."
                    ),
                )
            )
        return issues


@register
class RetryDelayMissingRule(Rule):
    """Retries without a delay hammer the failing dependency instantly."""

    id = "RETRY_DELAY_MISSING"
    severity = "WARNING"
    category = "reliability"
    formats = ORCHESTRATORS
    title = "Retries without retry_delay"
    description = (
        "Retries are configured but no retry_delay, so all attempts fire "
        "back-to-back and usually exhaust themselves before a transient "
        "outage clears."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag DAG defaults and tasks that retry with no delay."""
        issues: List[Issue] = []
        dag = _first_dag(pr)
        dag_retries = _dag_default_retries(pr)
        dag_delay = _dag_has_retry_delay(pr)

        if dag is not None and (dag_retries or 0) > 0 and not dag_delay:
            issues.append(
                self.issue(
                    f"DAG {_dag_id(dag)!r} sets retries={dag_retries} in "
                    "default_args but no retry_delay; retries fire immediately "
                    "and burn out before transient failures recover.",
                    line=_line(dag),
                    fix_suggestion=(
                        'Add "retry_delay": timedelta(minutes=5) (and consider '
                        "retry_exponential_backoff=True) next to the retries "
                        "setting in default_args."
                    ),
                )
            )

        offenders = []
        for task in _task_ops(pr):
            details = _details(task)
            retries = _as_int(details.get("retries"))
            if (retries or 0) > 0 and not details.get("has_retry_delay") and not dag_delay:
                offenders.append(task)
        if offenders:
            names = ", ".join(f"'{_task_id(t)}'" for t in offenders[:8])
            issues.append(
                self.issue(
                    f"Task(s) {names} configure retries but no retry_delay "
                    "(and the DAG default_args provide none), so retry "
                    "attempts run back-to-back.",
                    line=_line(offenders[0]),
                    fix_suggestion=(
                        "Add retry_delay=timedelta(minutes=5) to these tasks "
                        "or to the DAG default_args so retries actually wait "
                        "out transient failures."
                    ),
                )
            )
        return issues


@register
class NoExecutionTimeoutRule(Rule):
    """Heavy tasks without execution_timeout can hang forever."""

    id = "NO_EXECUTION_TIMEOUT"
    severity = "WARNING"
    category = "reliability"
    formats = ORCHESTRATORS
    title = "Heavy task without execution_timeout"
    description = (
        "A heavy task without execution_timeout can hang indefinitely on a "
        "stuck query or connection, holding its slot and stalling the run."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag each heavy task lacking an execution timeout."""
        issues: List[Issue] = []
        for task in _task_ops(pr):
            details = _details(task)
            if not details.get("is_heavy") or details.get("has_execution_timeout"):
                continue
            task_id = _task_id(task)
            operator = str(details.get("operator") or "task")
            issues.append(
                self.issue(
                    f"Heavy task {task_id!r} ({operator}) has no "
                    "execution_timeout; a stuck query or dropped connection "
                    "leaves it running forever.",
                    line=_line(task),
                    fix_suggestion=(
                        "Set execution_timeout=timedelta(hours=...) just above "
                        "the task's normal runtime so hangs fail fast and "
                        "retries can kick in."
                    ),
                )
            )
        return issues


# ---------------------------------------------------------------------------
# INFO rules
# ---------------------------------------------------------------------------

@register
class NoDagTagsRule(Rule):
    """Tags make DAGs findable in a busy Airflow UI."""

    id = "NO_DAG_TAGS"
    severity = "INFO"
    category = "maintainability"
    formats = ORCHESTRATORS
    title = "DAG has no tags"
    description = (
        "Untagged DAGs are hard to filter, group and audit once an Airflow "
        "instance hosts more than a handful of pipelines."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag DAGs with an empty/absent tags list."""
        issues: List[Issue] = []
        for dag in _dag_ops(pr):
            if _details(dag).get("tags"):
                continue
            issues.append(
                self.issue(
                    f"DAG {_dag_id(dag)!r} has no tags; tagging by team and "
                    "domain (e.g. tags=['finance', 'daily']) makes it "
                    "filterable in the UI and in audits.",
                    line=_line(dag),
                    fix_suggestion=(
                        "Add tags=[...] to the DAG constructor with the owning "
                        "team and data domain."
                    ),
                )
            )
        return issues


@register
class NoDocMdRule(Rule):
    """doc_md is the cheapest runbook a DAG can have."""

    id = "NO_DOC_MD"
    severity = "INFO"
    category = "maintainability"
    formats = AIRFLOW_ONLY
    title = "DAG has no doc_md"
    description = (
        "Without doc_md, on-call engineers opening the DAG in the UI get no "
        "context about what it does, who owns it, or how to rerun it safely."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag DAGs missing doc_md documentation."""
        issues: List[Issue] = []
        for dag in _dag_ops(pr):
            if _details(dag).get("has_doc_md"):
                continue
            issues.append(
                self.issue(
                    f"DAG {_dag_id(dag)!r} has no doc_md. A short markdown "
                    "blurb (purpose, owner, rerun instructions) shows up "
                    "directly in the Airflow UI where on-call needs it.",
                    line=_line(dag),
                    fix_suggestion=(
                        'Set dag.doc_md = """...""" (or doc_md= in the '
                        "constructor) describing purpose, inputs/outputs and "
                        "safe-rerun steps."
                    ),
                )
            )
        return issues


@register
class ManualOnlyScheduleRule(Rule):
    """A schedule of None means the DAG only runs when a human remembers."""

    id = "MANUAL_ONLY_SCHEDULE"
    severity = "INFO"
    category = "observability"
    formats = ORCHESTRATORS
    title = "Manual-only schedule"
    description = (
        "schedule=None means the DAG never runs automatically; fine for "
        "utility DAGs, but worth confirming for anything feeding production "
        "data."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag DAGs whose schedule is None (manual trigger only)."""
        issues: List[Issue] = []
        for dag in _dag_ops(pr):
            schedule = _details(dag).get("schedule")
            is_manual = schedule is None or (
                isinstance(schedule, str)
                and schedule.strip().lower() in {"", "none", "null"}
            )
            if not is_manual:
                continue
            issues.append(
                self.issue(
                    f"DAG {_dag_id(dag)!r} has schedule=None and only runs "
                    "when triggered manually. If consumers expect fresh data, "
                    "this is a silent staleness risk.",
                    line=_line(dag),
                    fix_suggestion=(
                        "If automatic refresh is expected, set a schedule "
                        '(cron string, "@daily", or a Dataset/asset trigger); '
                        "otherwise document that the DAG is manual-only."
                    ),
                )
            )
        return issues


@register
class SingleTaskDagRule(Rule):
    """One-task DAGs often hide a monolith that should be decomposed."""

    id = "SINGLE_TASK_DAG"
    severity = "INFO"
    category = "maintainability"
    formats = ORCHESTRATORS
    title = "DAG contains a single task"
    description = (
        "A DAG with one task usually wraps a monolithic script - you lose "
        "per-step retries, observability and partial reruns."
    )

    def check(self, pr: ParseResult) -> List[Issue]:
        """Flag DAGs whose task count is exactly one."""
        task_op_count = len(_task_ops(pr)) + len(_sensor_ops(pr))
        issues: List[Issue] = []
        for dag in _dag_ops(pr):
            count = _as_int(_details(dag).get("task_count"))
            if count is None:
                count = task_op_count
            if count != 1:
                continue
            issues.append(
                self.issue(
                    f"DAG {_dag_id(dag)!r} contains a single task. If that "
                    "task does extract + transform + load, a failure reruns "
                    "everything and you get no per-step visibility.",
                    line=_line(dag),
                    fix_suggestion=(
                        "Split the work into discrete tasks (extract, "
                        "transform, load, validate) so each step retries and "
                        "reports independently."
                    ),
                )
            )
        return issues
