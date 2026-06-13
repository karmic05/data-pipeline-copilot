"""FastAPI application for Data Pipeline Copilot.

Endpoints (see CONTRACTS.md):

- ``GET  /api/health``            -> ``{"status": "ok", "provider": ProviderInfo}``
- ``GET  /api/providers``         -> ``list[ProviderInfo]``
- ``POST /api/analyze``           -> ``AnalysisReport`` (400 on parse failure)
- ``POST /api/explain``           -> SSE stream of LLM tokens
- ``POST /api/explain/issue``     -> alias of ``/api/explain`` with ``task="issue"``
- ``GET  /api/analyze/{id}/stream?task=`` -> EventSource-compatible SSE stream
- ``POST /api/simulate/impact``   -> ``{"impacts": [ImpactResult], "cost": CostAnalysis}``
- ``POST /api/cost/estimate``     -> ``CostAnalysis``

SSE wire protocol (both stream endpoints)::

    data: {"token": "text chunk"}\\n\\n      (repeated)
    data: {"error": "message"}\\n\\n         (on failure, then done)
    data: {"done": true}\\n\\n               (terminal, always emitted)
"""
from __future__ import annotations

import json
import logging
from typing import AsyncIterator, Dict, List, Literal, Optional, Tuple

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app import store
from app.config import settings
from app.engines.cost import estimate_cost
from app.engines.impact import attach_impacts
from app.llm.client import get_provider_status, list_providers
from app.llm.tasks import stream_task
from app.schemas.ir import ParseError, ParseResult
from app.schemas.report import (
    AnalysisReport,
    CostAnalysis,
    ImpactResult,
    ProviderInfo,
    Warehouse,
)
from app.services.analyzer import analyze_full

logger = logging.getLogger(__name__)

app = FastAPI(title="Data Pipeline Copilot API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

LLMTask = Literal["explain", "issue", "optimize", "cost", "observability"]

_SSE_HEADERS: Dict[str, str] = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
}


# ── Request / response models (mirror frontend/lib/types.ts) ─────────────────


class AnalyzeRequest(BaseModel):
    """Body of ``POST /api/analyze`` — mirrors ``AnalyzeRequest`` in types.ts."""

    code: str
    format: str = "auto"
    dialect: Optional[str] = None
    row_count: int = 10_000_000
    daily_runs: int = 24
    warehouse: Warehouse = "snowflake"


class ExplainRequest(BaseModel):
    """Body of ``POST /api/explain`` — mirrors ``ExplainRequest`` in types.ts.

    ``analysis_id`` uses the in-memory store as a fast path. When the store
    misses (e.g. a serverless worker that never saw the original analysis) and
    ``code`` is supplied, the report is reconstructed by re-analyzing — this is
    what makes the backend safe to deploy statelessly. Issue ids are
    deterministic, so a re-analysis resolves ``issue_id`` correctly.
    """

    analysis_id: Optional[str] = None
    task: LLMTask = "explain"
    issue_id: Optional[str] = None
    code: Optional[str] = None
    format: str = "auto"
    dialect: Optional[str] = None
    row_count: int = 10_000_000
    daily_runs: int = 24
    warehouse: Warehouse = "snowflake"


class SimulateRequest(BaseModel):
    """Body of simulate/cost endpoints — mirrors ``SimulateRequest`` in types.ts.

    Like :class:`ExplainRequest`, this prefers the stored analysis but falls
    back to re-analyzing ``code`` when the store misses, so the simulate/cost
    endpoints work on stateless/serverless hosts.
    """

    analysis_id: Optional[str] = None
    row_count: int
    daily_runs: int
    warehouse: Warehouse
    code: Optional[str] = None
    format: str = "auto"
    dialect: Optional[str] = None


class SimulateResponse(BaseModel):
    """Response of ``POST /api/simulate/impact`` — mirrors types.ts."""

    impacts: List[ImpactResult]
    cost: CostAnalysis


class HealthResponse(BaseModel):
    """Response of ``GET /api/health`` — mirrors ``HealthResponse`` in types.ts."""

    status: str
    provider: ProviderInfo


# ── Helpers ───────────────────────────────────────────────────────────────────


def _reanalyze(
    code: str,
    *,
    format: str,
    dialect: Optional[str],
    row_count: int,
    daily_runs: int,
    warehouse: Warehouse,
) -> Tuple[AnalysisReport, ParseResult]:
    """Re-run the full analysis from raw code, mapping parse errors to 400."""
    try:
        report, pr = analyze_full(
            code,
            format=format or "auto",
            dialect=dialect,
            row_count=row_count,
            daily_runs=daily_runs,
            warehouse=warehouse,
        )
    except ParseError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    store.save(report, pr)
    return report, pr


def _resolve_pair(
    analysis_id: Optional[str],
    code: Optional[str],
    *,
    format: str,
    dialect: Optional[str],
    row_count: int,
    daily_runs: int,
    warehouse: Warehouse,
) -> Tuple[AnalysisReport, ParseResult]:
    """Resolve a (report, parse_result) pair from the store, else re-analyze.

    Single-container deployments hit the in-memory store fast path; stateless
    deployments fall through to re-analyzing ``code``. A 404 is raised only when
    neither a stored analysis nor source code is available.
    """
    if analysis_id:
        report = store.get(analysis_id)
        pr = store.get_parse_result(analysis_id)
        if report is not None and pr is not None:
            return report, pr
    if code:
        return _reanalyze(
            code,
            format=format,
            dialect=dialect,
            row_count=row_count,
            daily_runs=daily_runs,
            warehouse=warehouse,
        )
    raise HTTPException(
        status_code=404,
        detail=(
            "Unknown analysis_id and no code supplied. Re-run /api/analyze, or "
            "include the pipeline code in the request body."
        ),
    )


def _resolve_report(
    analysis_id: Optional[str],
    code: Optional[str],
    *,
    format: str,
    dialect: Optional[str],
    row_count: int,
    daily_runs: int,
    warehouse: Warehouse,
) -> AnalysisReport:
    """Resolve a report from the store, else re-analyze ``code`` (or 404)."""
    if analysis_id:
        report = store.get(analysis_id)
        if report is not None:
            return report
    if code:
        report, _ = _reanalyze(
            code,
            format=format,
            dialect=dialect,
            row_count=row_count,
            daily_runs=daily_runs,
            warehouse=warehouse,
        )
        return report
    raise HTTPException(
        status_code=404,
        detail=(
            "Unknown analysis_id and no code supplied. Re-run /api/analyze, or "
            "include the pipeline code in the request body."
        ),
    )


def _sse(payload: dict) -> str:
    """Encode one SSE event line (``data: <json>\\n\\n``)."""
    return f"data: {json.dumps(payload, default=str)}\n\n"


async def _sse_events(
    task: str, report: AnalysisReport, issue_id: Optional[str]
) -> AsyncIterator[str]:
    """Stream LLM task tokens as SSE events, always ending with ``done``."""
    try:
        async for token in stream_task(task, report, issue_id=issue_id):
            yield _sse({"token": token})
    except Exception as exc:
        logger.exception("LLM stream failed (task=%s, analysis=%s)", task, report.id)
        yield _sse({"error": str(exc) or type(exc).__name__})
    yield _sse({"done": True})


def _stream_response(
    task: str, report: AnalysisReport, issue_id: Optional[str]
) -> StreamingResponse:
    """Wrap the SSE generator in a properly headed streaming response."""
    return StreamingResponse(
        _sse_events(task, report, issue_id),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


# ── Routes ────────────────────────────────────────────────────────────────────


@app.get("/")
def root() -> Dict[str, str]:
    """Service banner with a pointer to the interactive docs."""
    return {"service": "Data Pipeline Copilot API", "docs": "/docs"}


@app.get("/api/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Liveness check plus the active LLM provider status."""
    return HealthResponse(status="ok", provider=get_provider_status())


@app.get("/api/providers", response_model=List[ProviderInfo])
def providers() -> List[ProviderInfo]:
    """Status of every supported LLM provider."""
    return list_providers()


@app.post("/api/analyze", response_model=AnalysisReport)
def analyze_endpoint(req: AnalyzeRequest) -> AnalysisReport:
    """Run the full deterministic analysis and persist it for follow-ups."""
    try:
        report, pr = analyze_full(
            req.code,
            format=req.format or "auto",
            dialect=req.dialect,
            row_count=req.row_count,
            daily_runs=req.daily_runs,
            warehouse=req.warehouse,
        )
    except ParseError as exc:
        logger.info("Parse failed: %s", exc.message)
        raise HTTPException(status_code=400, detail=exc.message) from exc
    store.save(report, pr)
    return report


@app.post("/api/explain")
def explain(req: ExplainRequest) -> StreamingResponse:
    """Stream an LLM task (explain/issue/optimize/cost/observability) over SSE."""
    report = _resolve_report(
        req.analysis_id,
        req.code,
        format=req.format,
        dialect=req.dialect,
        row_count=req.row_count,
        daily_runs=req.daily_runs,
        warehouse=req.warehouse,
    )
    return _stream_response(req.task, report, req.issue_id)


@app.post("/api/explain/issue")
def explain_issue(req: ExplainRequest) -> StreamingResponse:
    """Alias of ``/api/explain`` that forces ``task="issue"``."""
    report = _resolve_report(
        req.analysis_id,
        req.code,
        format=req.format,
        dialect=req.dialect,
        row_count=req.row_count,
        daily_runs=req.daily_runs,
        warehouse=req.warehouse,
    )
    return _stream_response("issue", report, req.issue_id)


@app.get("/api/analyze/{analysis_id}/stream")
def analyze_stream(
    analysis_id: str,
    task: LLMTask = "explain",
    issue_id: Optional[str] = None,
) -> StreamingResponse:
    """EventSource-compatible GET variant of the SSE explain stream.

    EventSource cannot send a request body, so this variant is store-only and
    will 404 if the analysis is not held by the serving worker. The POST
    ``/api/explain`` route is the stateless-capable path used by the frontend.
    """
    report = store.get(analysis_id)
    if report is None:
        raise HTTPException(
            status_code=404, detail=f"Unknown analysis_id: {analysis_id}"
        )
    return _stream_response(task, report, issue_id)


@app.post("/api/simulate/impact", response_model=SimulateResponse)
def simulate_impact(req: SimulateRequest) -> SimulateResponse:
    """Re-run cost + impact engines against an analysis with new scale params."""
    report, pr = _resolve_pair(
        req.analysis_id,
        req.code,
        format=req.format,
        dialect=req.dialect,
        row_count=req.row_count,
        daily_runs=req.daily_runs,
        warehouse=req.warehouse,
    )
    cost = estimate_cost(
        pr,
        report.issues,
        row_count=req.row_count,
        daily_runs=req.daily_runs,
        warehouse=req.warehouse,
    )
    issues = [issue.model_copy(deep=True) for issue in report.issues]
    impacts = attach_impacts(
        issues,
        row_count=req.row_count,
        daily_runs=req.daily_runs,
        warehouse=req.warehouse,
        base_monthly_cost=cost.monthly_usd_current,
    )
    return SimulateResponse(impacts=impacts, cost=cost)


@app.post("/api/cost/estimate", response_model=CostAnalysis)
def cost_estimate(req: SimulateRequest) -> CostAnalysis:
    """Re-run the cost engine alone against an analysis with new scale params."""
    report, pr = _resolve_pair(
        req.analysis_id,
        req.code,
        format=req.format,
        dialect=req.dialect,
        row_count=req.row_count,
        daily_runs=req.daily_runs,
        warehouse=req.warehouse,
    )
    return estimate_cost(
        pr,
        report.issues,
        row_count=req.row_count,
        daily_runs=req.daily_runs,
        warehouse=req.warehouse,
    )
