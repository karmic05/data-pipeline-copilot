"""Strict output schemas for every analysis response.

The frontend TypeScript types in ``frontend/lib/types.ts`` mirror these models
field-for-field (snake_case preserved). Any change here must be mirrored there.
"""
from __future__ import annotations

from typing import Dict, List, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

from app.schemas.ir import IR, Location, PipelineFormat

Severity = Literal["CRITICAL", "WARNING", "INFO"]
RiskLevel = Literal["HIGH", "MEDIUM", "LOW"]
IssueCategory = Literal[
    "performance",
    "reliability",
    "observability",
    "maintainability",
    "security",
    "cost",
]
Warehouse = Literal["snowflake", "bigquery", "redshift", "databricks"]

SEVERITY_ORDER: Dict[str, int] = {"CRITICAL": 0, "WARNING": 1, "INFO": 2}


class IssueImpact(BaseModel):
    latency_pct: float = 0.0
    cost_multiplier: float = 1.0
    failure_probability: float = 0.0


class Issue(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    rule: str
    severity: Severity
    category: IssueCategory
    title: str
    message: str
    location: Optional[Location] = None
    fix_suggestion: Optional[str] = None
    fix_diff: Optional[str] = None
    impact: Optional[IssueImpact] = None


class CostProjections(BaseModel):
    d30_current: float = 0.0
    d90_current: float = 0.0
    d365_current: float = 0.0
    d30_optimized: float = 0.0
    d90_optimized: float = 0.0
    d365_optimized: float = 0.0


class CostAnalysis(BaseModel):
    provider: Warehouse = "snowflake"
    credits_per_run: float = 0.0
    bytes_billed: int = 0
    monthly_usd_current: float = 0.0
    monthly_usd_optimized: float = 0.0
    savings_pct: float = 0.0
    risk_level: RiskLevel = "LOW"
    projections: CostProjections = Field(default_factory=CostProjections)
    reasoning: List[str] = Field(default_factory=list)


class LineageColumnLink(BaseModel):
    from_column: str
    to_column: str
    transformation: str = "direct"


class LineageNode(BaseModel):
    id: str
    label: str
    type: Literal["source", "model", "table", "task", "output"] = "table"
    schema_name: Optional[str] = None
    columns: List[str] = Field(default_factory=list)


class LineageEdge(BaseModel):
    id: str
    source: str
    target: str
    transformation: str = "direct"
    column_links: List[LineageColumnLink] = Field(default_factory=list)


class LineageGraph(BaseModel):
    nodes: List[LineageNode] = Field(default_factory=list)
    edges: List[LineageEdge] = Field(default_factory=list)


class RoadmapItem(BaseModel):
    dimension: str
    action: str
    estimated_gain: float = 0.0


class ProductionScore(BaseModel):
    total: float = 0.0
    efficiency: float = 0.0
    reliability: float = 0.0
    observability: float = 0.0
    maintainability: float = 0.0
    security: float = 0.0
    roadmap: List[RoadmapItem] = Field(default_factory=list)


class ImpactResult(BaseModel):
    issue_id: str
    rule: str
    latency_increase_pct: float = 0.0
    monthly_cost_increase_usd: float = 0.0
    failure_probability_per_run: float = 0.0
    pagerduty_incidents_per_month: float = 0.0
    human_summary: str = ""


class GeneratedTest(BaseModel):
    framework: Literal["dbt", "great_expectations"]
    target: str
    name: str
    description: str = ""


class GeneratedTests(BaseModel):
    dbt_yaml: str = ""
    great_expectations_json: str = ""
    tests: List[GeneratedTest] = Field(default_factory=list)
    coverage_gaps: List[str] = Field(default_factory=list)


class PIIColumn(BaseModel):
    table: str
    column: str
    pii_type: str
    confidence: float = 0.5
    recommendation: str = ""


class SecurityReport(BaseModel):
    risk_level: RiskLevel = "LOW"
    pii_columns: List[PIIColumn] = Field(default_factory=list)
    findings: List[str] = Field(default_factory=list)


class ProviderInfo(BaseModel):
    provider: str
    model: str
    available: bool = False
    detail: str = ""


class AnalysisParams(BaseModel):
    row_count: int = 10_000_000
    daily_runs: int = 24
    warehouse: Warehouse = "snowflake"


class AnalysisReport(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    created_at: str = ""
    format: PipelineFormat
    dialect: Optional[str] = None
    summary: str = ""
    issues: List[Issue] = Field(default_factory=list)
    optimizations: List[str] = Field(default_factory=list)
    cost_analysis: CostAnalysis = Field(default_factory=CostAnalysis)
    lineage: LineageGraph = Field(default_factory=LineageGraph)
    production_score: ProductionScore = Field(default_factory=ProductionScore)
    impacts: List[ImpactResult] = Field(default_factory=list)
    generated_tests: GeneratedTests = Field(default_factory=GeneratedTests)
    security: SecurityReport = Field(default_factory=SecurityReport)
    params: AnalysisParams = Field(default_factory=AnalysisParams)
    parser_warnings: List[str] = Field(default_factory=list)
    ir: Optional[IR] = None
