"""Durable, inspectable, step-based workflow engine (the "swf").

This module is a small but genuinely reusable orchestration primitive. A
:class:`WorkflowEngine` runs an ordered list of named async steps over a shared
mutable context dict and produces a fully inspectable execution record - one
:class:`~app.schemas.agent.WorkflowStep` per step, with timing, status, retry
count and a human-readable detail string. Nothing here is specific to the data
pipeline agent; any sequence of async stages can be driven by it.

Why "durable"?
--------------
Durability here means the run is *recorded and recoverable as data*, not that it
checkpoints to disk. Every step transition is captured in a
:class:`WorkflowStep` the moment it happens, so a partially-completed run is
always fully described by ``engine.steps``: which step ran, how long it took,
how many attempts it cost, whether it succeeded/failed/was skipped, and why.
That record is the contract the agent serializes into an ``AgentRun``; a caller
could persist it after every step and resume/inspect later. The engine itself
never raises out of :meth:`run` - a failing step is captured, not propagated.

Core guarantees
---------------
- **Ordered.** Steps run in registration order.
- **Timed.** Wall-clock duration via :func:`time.monotonic`; ISO-8601 UTC
  ``started_at`` / ``finished_at`` timestamps.
- **Retried.** Each step may declare ``max_attempts``; an exception retries up
  to that bound, and the final ``attempts`` count is recorded.
- **Skippable.** A step may short-circuit itself by raising :class:`SkipStep`
  (recorded ``skipped``, not a failure) or returning a :class:`StepResult` with
  ``status="skipped"``.
- **Policy-driven failure handling.** When a step exhausts its retries the
  engine consults the step's :class:`FailurePolicy`: ``CONTINUE`` records the
  failure and proceeds; ``ABORT`` records the failure and marks every remaining
  step ``skipped`` (used for hard failures such as a parse error).
- **Shared context.** Steps read/write a single mutable ``context`` dict to pass
  data forward.

Each step function is ``async def step(ctx: dict) -> StepResult | None``. It may
mutate ``ctx`` freely. Returning ``None`` (or any non-:class:`StepResult` value)
is treated as success with no detail; returning a :class:`StepResult` lets the
step attach a ``detail`` string or declare itself ``skipped``.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional

from app.schemas.agent import WorkflowStep

logger = logging.getLogger(__name__)

#: A step coroutine: receives the shared context, optionally returns a result.
StepFn = Callable[[Dict[str, Any]], Awaitable[Optional["StepResult"]]]


def _now_iso() -> str:
    """Current time as an ISO-8601 string in UTC (the timestamp contract)."""
    return datetime.now(timezone.utc).isoformat()


class SkipStep(Exception):
    """Raised inside a step to mark it ``skipped`` (not failed).

    The optional message becomes the recorded step ``detail`` - e.g. a step that
    has no work to do because an upstream feature is disabled.
    """

    def __init__(self, reason: str = "") -> None:
        super().__init__(reason)
        self.reason = reason


class FailurePolicy(str, Enum):
    """What the engine does after a step exhausts its retries and fails."""

    #: Record the failure and run the remaining steps anyway.
    CONTINUE = "continue"
    #: Record the failure and mark every remaining step ``skipped``.
    ABORT = "abort"


@dataclass
class StepResult:
    """Optional return value from a step, carrying status + detail.

    A step may simply return ``None`` (treated as success). Returning a
    ``StepResult`` lets it attach a human-readable ``detail`` or declare itself
    ``skipped`` without raising :class:`SkipStep`.
    """

    status: str = "success"  # "success" | "skipped"
    detail: str = ""


@dataclass
class Step:
    """A registered, named step in the workflow."""

    name: str
    fn: StepFn
    label: str = ""
    max_attempts: int = 1
    #: Per-step policy applied when the step fails after all retries.
    on_failure: FailurePolicy = FailurePolicy.CONTINUE
    #: Optional delay (seconds) between retry attempts.
    retry_delay: float = 0.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            self.max_attempts = 1
        if not self.label:
            self.label = self.name.replace("_", " ").title()


@dataclass
class WorkflowEngine:
    """An ordered runner of named async steps with a recorded execution trace.

    Construct it (optionally naming the workflow), register steps with
    :meth:`add_step`, then ``await engine.run()``. After the run, ``engine.steps``
    holds one :class:`WorkflowStep` per registered step and ``engine.context``
    holds the shared data the steps produced.

    The engine is single-use per :meth:`run` call but reusable as a *definition*:
    the registered :class:`Step` list is the reusable workflow; calling
    :meth:`run` again resets the recorded trace and re-executes them.
    """

    name: str = "workflow"
    _steps: List[Step] = field(default_factory=list)
    context: Dict[str, Any] = field(default_factory=dict)
    _records: List[WorkflowStep] = field(default_factory=list)
    aborted: bool = False

    # -- definition -----------------------------------------------------------
    def add_step(
        self,
        name: str,
        fn: StepFn,
        *,
        label: str = "",
        max_attempts: int = 1,
        on_failure: FailurePolicy = FailurePolicy.CONTINUE,
        retry_delay: float = 0.0,
    ) -> "WorkflowEngine":
        """Register a step. Returns ``self`` so registrations can be chained."""
        self._steps.append(
            Step(
                name=name,
                fn=fn,
                label=label,
                max_attempts=max_attempts,
                on_failure=on_failure,
                retry_delay=retry_delay,
            )
        )
        return self

    # -- inspection -----------------------------------------------------------
    @property
    def steps(self) -> List[WorkflowStep]:
        """The recorded step trace (one entry per registered step)."""
        return self._records

    def step(self, name: str) -> Optional[WorkflowStep]:
        """The recorded :class:`WorkflowStep` for ``name``, if it ran."""
        for record in self._records:
            if record.name == name:
                return record
        return None

    @property
    def succeeded(self) -> bool:
        """Whether the run completed with no failed step and no abort."""
        return not self.aborted and all(
            record.status != "failed" for record in self._records
        )

    # -- execution ------------------------------------------------------------
    async def run(self, context: Optional[Dict[str, Any]] = None) -> List[WorkflowStep]:
        """Run every registered step in order; return the recorded trace.

        ``context`` seeds the shared mutable dict passed to each step (a fresh
        ``{}`` if omitted). Re-running resets the recorded trace. This method
        never raises: a step exception becomes a recorded ``failed`` step, and a
        step with an ``ABORT`` policy short-circuits the remaining steps to
        ``skipped``.
        """
        self.context = context if context is not None else {}
        self._records = []
        self.aborted = False

        for index, step in enumerate(self._steps):
            if self.aborted:
                self._records.append(self._skipped_record(step, "Skipped: earlier step aborted the run."))
                continue
            record = await self._run_step(step)
            self._records.append(record)
            if record.status == "failed" and step.on_failure is FailurePolicy.ABORT:
                self.aborted = True
                logger.info(
                    "Workflow %r aborting after hard failure in step %r; "
                    "remaining steps will be skipped",
                    self.name,
                    step.name,
                )
        return self._records

    async def _run_step(self, step: Step) -> WorkflowStep:
        """Execute one step with timing + retries; return its recorded entry."""
        record = WorkflowStep(
            name=step.name,
            label=step.label,
            status="running",
            started_at=_now_iso(),
            attempts=0,
        )
        start = time.monotonic()
        last_error: Optional[BaseException] = None

        for attempt in range(1, step.max_attempts + 1):
            record.attempts = attempt
            try:
                result = await step.fn(self.context)
            except SkipStep as skip:
                record.status = "skipped"
                record.detail = skip.reason or "Step skipped."
                return self._finalize(record, start)
            except Exception as exc:  # a real error - maybe retry
                last_error = exc
                logger.warning(
                    "Workflow %r step %r failed on attempt %d/%d: %s",
                    self.name,
                    step.name,
                    attempt,
                    step.max_attempts,
                    exc,
                )
                if attempt < step.max_attempts:
                    if step.retry_delay > 0:
                        await asyncio.sleep(step.retry_delay)
                    continue
                record.status = "failed"
                record.detail = self._error_detail(exc)
                return self._finalize(record, start)
            else:
                # Success (possibly self-declared skip via StepResult).
                if isinstance(result, StepResult) and result.status == "skipped":
                    record.status = "skipped"
                    record.detail = result.detail or "Step skipped."
                else:
                    record.status = "success"
                    if isinstance(result, StepResult):
                        record.detail = result.detail
                return self._finalize(record, start)

        # Defensive: the loop always returns above, but keep types honest.
        record.status = "failed"
        record.detail = self._error_detail(last_error) if last_error else "Unknown failure."
        return self._finalize(record, start)

    @staticmethod
    def _finalize(record: WorkflowStep, start: float) -> WorkflowStep:
        """Stamp ``finished_at`` and ``duration_ms`` from the monotonic start."""
        record.finished_at = _now_iso()
        record.duration_ms = int(round((time.monotonic() - start) * 1000))
        return record

    @staticmethod
    def _skipped_record(step: Step, reason: str) -> WorkflowStep:
        """A zero-duration ``skipped`` record for a step never attempted."""
        stamp = _now_iso()
        return WorkflowStep(
            name=step.name,
            label=step.label,
            status="skipped",
            started_at=stamp,
            finished_at=stamp,
            duration_ms=0,
            attempts=0,
            detail=reason,
        )

    @staticmethod
    def _error_detail(exc: BaseException) -> str:
        """Compact, human-readable detail string for a failed step."""
        message = str(exc).strip() or exc.__class__.__name__
        return f"{exc.__class__.__name__}: {message}"
