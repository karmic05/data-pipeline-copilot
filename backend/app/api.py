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
import time as _time
from collections import defaultdict, deque
from typing import AsyncIterator, Deque, Dict, List, Literal, Optional, Tuple

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from app import store
from app.agent import run_agent
from app.config import settings
from app.engines.cost import estimate_cost
from app.engines.dynamic import dynamic_review
from app.engines.impact import attach_impacts
from app.llm.client import LLMUnavailable, get_provider_status, list_providers
from app.llm.tasks import stream_task
from app.schemas.agent import AgentRun, AgentRunRequest, GenerateRequest, GenerateResponse
from app.schemas.ir import ParseError, ParseResult
from app.schemas.report import (
    SEVERITY_ORDER,
    AnalysisReport,
    CostAnalysis,
    ImpactResult,
    ProviderInfo,
    Warehouse,
)
from app.services.analyzer import analyze_full
from app.services.generator import generate_pipeline

logger = logging.getLogger(__name__)

app = FastAPI(title="Data Pipeline Copilot API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── API security: optional API key + per-IP rate limiting ─────────────────────
# Rate limiting is always on (sliding 60s window per client IP). When
# ``settings.api_key`` is set, mutating POST /api/* requests must carry a
# matching ``X-API-Key`` header. Defaults keep the public demo open; an operator
# locks it down by setting API_KEY (and optionally RATE_LIMIT_PER_MIN).
_RATE_BUCKETS: Dict[str, Deque[float]] = defaultdict(deque)
_RATE_WINDOW_S = 60.0


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    path = request.url.path
    if request.method == "POST" and path.startswith("/api/"):
        if settings.api_key and request.headers.get("x-api-key", "") != settings.api_key:
            return JSONResponse(
                {"detail": "Invalid or missing API key (set the X-API-Key header)."},
                status_code=401,
            )
        ip = _client_ip(request)
        now = _time.monotonic()
        bucket = _RATE_BUCKETS[ip]
        while bucket and now - bucket[0] > _RATE_WINDOW_S:
            bucket.popleft()
        if len(bucket) >= settings.rate_limit_per_min:
            return JSONResponse(
                {"detail": "Rate limit exceeded — slow down and retry shortly."},
                status_code=429,
                headers={"Retry-After": "60"},
            )
        bucket.append(now)
        if len(_RATE_BUCKETS) > 10_000:  # guard against unbounded IP accumulation
            for k in [k for k, v in _RATE_BUCKETS.items() if not v]:
                _RATE_BUCKETS.pop(k, None)
    return await call_next(request)

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
    #: When true (and a provider is available), run the advisory LLM
    #: dynamic-review pass and merge its findings (source="dynamic").
    dynamic: bool = False


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
async def analyze_endpoint(req: AnalyzeRequest) -> AnalysisReport:
    """Run the full deterministic analysis and persist it for follow-ups.

    When ``req.dynamic`` is set, an advisory LLM "dynamic review" pass runs on
    top of the deterministic rules and its findings (``source="dynamic"``) are
    merged in. The deterministic rules remain the source of truth; the dynamic
    pass only adds and never overrides.
    """
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

    if req.dynamic:
        try:
            extra = await dynamic_review(report, pr)
            if extra:
                report.issues.extend(extra)
                report.issues.sort(
                    key=lambda i: (
                        SEVERITY_ORDER.get(i.severity, 9),
                        i.location.line if i.location else 10**9,
                    )
                )
        except Exception:  # advisory pass must never fail the analysis
            logger.exception("Dynamic review failed; returning rule findings only")

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


@app.post("/api/generate", response_model=GenerateResponse)
async def generate_endpoint(req: GenerateRequest) -> GenerateResponse:
    """Generate pipeline code from a plain-English description.

    Uses the configured LLM provider; degrades to a deterministic templated
    skeleton (with a note) when no provider is available.
    """
    if not req.prompt or not req.prompt.strip():
        raise HTTPException(status_code=400, detail="Describe the pipeline to generate.")
    try:
        return await generate_pipeline(req)
    except LLMUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/agent/run", response_model=AgentRun)
async def agent_run_endpoint(req: AgentRunRequest) -> AgentRun:
    """Run the autonomous pipeline-doctor agent over a durable workflow.

    Steps (analyze -> dynamic review -> propose fixes -> apply -> re-analyze ->
    measure) are each recorded with timing/retries, and the run reports both
    operational (agent) KPIs and business KPIs (score lift, $ saved, incidents
    prevented). Never raises — a failed run is returned with ``status="failed"``.
    """
    if not req.code or not req.code.strip():
        raise HTTPException(status_code=400, detail="Provide pipeline code for the agent to analyze.")
    return await run_agent(req)
