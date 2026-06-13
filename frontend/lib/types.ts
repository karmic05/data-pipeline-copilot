/**
 * Mirrors backend/app/schemas/report.py and ir.py field-for-field
 * (snake_case preserved). Any change there must be mirrored here.
 */

export type PipelineFormat =
  | "sql"
  | "airflow"
  | "dbt"
  | "spark"
  | "prefect"
  | "flink"
  | "kafka"
  | "great_expectations";

export type Severity = "CRITICAL" | "WARNING" | "INFO";
export type RiskLevel = "HIGH" | "MEDIUM" | "LOW";
export type Warehouse = "snowflake" | "bigquery" | "redshift" | "databricks";
export type IssueCategory =
  | "performance"
  | "reliability"
  | "observability"
  | "maintainability"
  | "security"
  | "cost";

export interface Location {
  line: number;
  col: number;
}

export interface IssueImpact {
  latency_pct: number;
  cost_multiplier: number;
  failure_probability: number;
}

export interface Issue {
  id: string;
  rule: string;
  severity: Severity;
  category: IssueCategory;
  title: string;
  message: string;
  location: Location | null;
  fix_suggestion: string | null;
  fix_diff: string | null;
  impact: IssueImpact | null;
}

export interface CostProjections {
  d30_current: number;
  d90_current: number;
  d365_current: number;
  d30_optimized: number;
  d90_optimized: number;
  d365_optimized: number;
}

export interface CostAnalysis {
  provider: Warehouse;
  credits_per_run: number;
  bytes_billed: number;
  monthly_usd_current: number;
  monthly_usd_optimized: number;
  savings_pct: number;
  risk_level: RiskLevel;
  projections: CostProjections;
  reasoning: string[];
}

export interface LineageColumnLink {
  from_column: string;
  to_column: string;
  transformation: string;
}

export type LineageNodeType = "source" | "model" | "table" | "task" | "output";

export interface LineageNode {
  id: string;
  label: string;
  type: LineageNodeType;
  schema_name: string | null;
  columns: string[];
}

export interface LineageEdge {
  id: string;
  source: string;
  target: string;
  transformation: string;
  column_links: LineageColumnLink[];
}

export interface LineageGraph {
  nodes: LineageNode[];
  edges: LineageEdge[];
}

export interface RoadmapItem {
  dimension: string;
  action: string;
  estimated_gain: number;
}

export interface ProductionScore {
  total: number;
  efficiency: number;
  reliability: number;
  observability: number;
  maintainability: number;
  security: number;
  roadmap: RoadmapItem[];
}

export interface ImpactResult {
  issue_id: string;
  rule: string;
  latency_increase_pct: number;
  monthly_cost_increase_usd: number;
  failure_probability_per_run: number;
  pagerduty_incidents_per_month: number;
  human_summary: string;
}

export interface GeneratedTest {
  framework: "dbt" | "great_expectations";
  target: string;
  name: string;
  description: string;
}

export interface GeneratedTests {
  dbt_yaml: string;
  great_expectations_json: string;
  tests: GeneratedTest[];
  coverage_gaps: string[];
}

export interface PIIColumn {
  table: string;
  column: string;
  pii_type: string;
  confidence: number;
  recommendation: string;
}

export interface SecurityReport {
  risk_level: RiskLevel;
  pii_columns: PIIColumn[];
  findings: string[];
}

export interface ProviderInfo {
  provider: string;
  model: string;
  available: boolean;
  detail: string;
}

export interface AnalysisParams {
  row_count: number;
  daily_runs: number;
  warehouse: Warehouse;
}

export interface AnalysisReport {
  id: string;
  created_at: string;
  format: PipelineFormat;
  dialect: string | null;
  summary: string;
  issues: Issue[];
  optimizations: string[];
  cost_analysis: CostAnalysis;
  lineage: LineageGraph;
  production_score: ProductionScore;
  impacts: ImpactResult[];
  generated_tests: GeneratedTests;
  security: SecurityReport;
  params: AnalysisParams;
  parser_warnings: string[];
  ir: unknown | null;
}

// ── API request shapes ───────────────────────────────────────────────────────

export interface AnalyzeRequest {
  code: string;
  format?: string; // "auto" (default) or explicit PipelineFormat
  dialect?: string | null;
  row_count?: number;
  daily_runs?: number;
  warehouse?: Warehouse;
}

export type LLMTask = "explain" | "issue" | "optimize" | "cost" | "observability";

export interface ExplainRequest {
  analysis_id: string;
  task: LLMTask;
  issue_id?: string;
  // Sent so the backend can re-analyze if the in-memory store missed it
  // (e.g. on a serverless host). Harmless on single-container deployments.
  code?: string;
}

export interface SimulateRequest {
  analysis_id: string;
  row_count: number;
  daily_runs: number;
  warehouse: Warehouse;
  // See ExplainRequest.code — enables stateless re-analysis fallback.
  code?: string;
}

export interface SimulateResponse {
  impacts: ImpactResult[];
  cost: CostAnalysis;
}

export interface HealthResponse {
  status: string;
  provider: ProviderInfo;
}

export const SEVERITY_ORDER: Record<Severity, number> = {
  CRITICAL: 0,
  WARNING: 1,
  INFO: 2,
};

export const TAB_IDS = [
  "score",
  "issues",
  "lineage",
  "cost",
  "impact",
  "observability",
  "security",
] as const;

export type TabId = (typeof TAB_IDS)[number];
