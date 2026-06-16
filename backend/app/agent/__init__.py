"""The autonomous Data Pipeline Copilot agent and its durable workflow engine.

- :func:`run_agent` runs the full agent workflow over a pipeline and returns an
  inspectable :class:`~app.schemas.agent.AgentRun` with operational + business
  KPIs.
- :class:`WorkflowEngine` is the reusable durable, step-based engine that drives
  the run (the "swf").
"""
from __future__ import annotations

from app.agent.runner import run_agent
from app.agent.workflow import (
    FailurePolicy,
    SkipStep,
    Step,
    StepResult,
    WorkflowEngine,
)

__all__ = [
    "run_agent",
    "WorkflowEngine",
    "Step",
    "StepResult",
    "SkipStep",
    "FailurePolicy",
]
