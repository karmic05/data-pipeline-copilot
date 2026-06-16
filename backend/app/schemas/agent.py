"""Schemas for the durable agent workflow, KPIs, and the text->pipeline generator.

The agent runs a deterministic, durable, step-based workflow over a pipeline
(parse -> analyze -> dynamic review -> propose fixes -> apply -> re-analyze ->
measure). Each step is recorded so a run is fully inspectable and resumable.
Mirrored in ``frontend/lib/types.ts``.
"""
from __future__ import annotations

from typing import List, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

from app.schemas.connectors import ConnectorConfig
from app.schemas.report import AnalysisReport, Warehouse

# ── Text -> pipeline generator ───────────────────────────────────────────────


class GenerateRequest(BaseModel):
    """Body of ``POST /api/generate`` — plain-English -> pipeline code."""

    prompt: str
    target_format: str = "auto"  # auto | sql | dbt | airflow | spark | flink
    dialect: Optional[str] = None  # snowflake | bigquery | postgres | ...


class GenerateResponse(BaseModel):
    code: str
    format: str
    dialect: Optional[str] = None
    notes: List[str] = Field(default_factory=list)


# ── Durable workflow steps ───────────────────────────────────────────────────

StepStatus = Literal["pending", "running", "success", "failed", "skipped"]


class WorkflowStep(BaseModel):
    name: str
    label: str = ""
    status: StepStatus = "pending"
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    duration_ms: Optional[int] = None
    attempts: int = 1
    detail: str = ""


# ── KPIs ─────────────────────────────────────────────────────────────────────


class AgentKPIs(BaseModel):
    """Operational KPIs for the agent run itself."""

    steps_total: int = 0
    steps_succeeded: int = 0
    steps_failed: int = 0
    retries: int = 0
    duration_ms: int = 0
    fixes_proposed: int = 0
    fixes_applied: int = 0
    success: bool = False
    used_llm: bool = False


class BusinessKPIs(BaseModel):
    """Business value the agent delivered (before vs after the fix)."""

    score_before: float = 0.0
    score_after: float = 0.0
    score_delta: float = 0.0
    issues_before: int = 0
    issues_after: int = 0
    issues_resolved: int = 0
    critical_before: int = 0
    critical_after: int = 0
    monthly_cost_before: float = 0.0
    monthly_cost_after: float = 0.0
    monthly_savings: float = 0.0
    savings_pct: float = 0.0
    annual_savings: float = 0.0
    projected_incidents_prevented: float = 0.0


class AgentRun(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    created_at: str = ""
    status: Literal["completed", "failed", "partial"] = "completed"
    steps: List[WorkflowStep] = Field(default_factory=list)
    agent_kpis: AgentKPIs = Field(default_factory=AgentKPIs)
    business_kpis: BusinessKPIs = Field(default_factory=BusinessKPIs)
    initial_report: Optional[AnalysisReport] = None
    final_report: Optional[AnalysisReport] = None
    fixed_code: Optional[str] = None
    summary: str = ""


class AgentRunRequest(BaseModel):
    code: str
    format: str = "auto"
    dialect: Optional[str] = None
    row_count: int = 10_000_000
    daily_runs: int = 24
    warehouse: Warehouse = "snowflake"
    apply_fixes: bool = True
    #: Optional live DB connection — the agent adds a "ground_with_db" step that
    #: resolves real schemas + real profiled cost before proposing fixes.
    connection: Optional[ConnectorConfig] = None
