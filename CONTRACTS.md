# Data Pipeline Copilot — Module Contracts

This document is the single source of truth for cross-module interfaces.
**Every implementer must read this file plus the contract source files before
writing code, follow the signatures exactly, and write ONLY their assigned
files.** Contract source files (already written, do not modify):

- `backend/app/schemas/ir.py` — IR, ParseResult, ParseError, Location, Operation vocabulary
- `backend/app/schemas/report.py` — Issue, CostAnalysis, LineageGraph, ProductionScore, ImpactResult, GeneratedTests, SecurityReport, AnalysisReport, ProviderInfo, AnalysisParams
- `backend/app/parsers/__init__.py` — detect_format + parse dispatcher (calls the functions below)
- `backend/app/rules/__init__.py` — Rule base class, @register, run_rules
- `frontend/lib/types.ts` — TS mirror of report schemas + request shapes + TabId
- `frontend/lib/api.ts` — analyze/simulateImpact/estimateCost/getHealth/getProviders/streamTask
- `frontend/lib/store.tsx` — AnalysisProvider / useAnalysis()

Architecture: deterministic parser → IR (Pydantic-validated) → rule engine
(source of truth) → engines (cost/lineage/score/impact/observability/security)
→ AnalysisReport. The LLM layer receives the IR/report only — never raw code —
and only explains/suggests. All LLM output streams over SSE.

---

## Backend module signatures

### Parsers (each returns `ParseResult`, raises `ParseError` on bad input)

```python
# app/parsers/sql_parser.py
def parse_sql(source: str, dialect: str | None) -> ParseResult: ...
def parse_flink(source: str) -> ParseResult: ...

# app/parsers/airflow_parser.py
def parse_airflow(source: str) -> ParseResult: ...
def parse_prefect(source: str) -> ParseResult: ...   # Prefect AND Dagster

# app/parsers/spark_parser.py
def parse_spark(source: str) -> ParseResult: ...

# app/parsers/streaming_parser.py
def parse_kafka(source: str) -> ParseResult: ...

# app/parsers/dbt_parser.py
def parse_dbt(source: str, dialect: str | None) -> ParseResult: ...

# app/parsers/ge_parser.py
def parse_great_expectations(source: str) -> ParseResult: ...
```

Parser conventions:
- `ParseResult.ast` = list of sqlglot Expressions (SQL/dbt/Flink) or `ast.Module` (Python formats) or dict (YAML formats).
- Populate `ir.tables` (with `access_type`), `ir.operations` (use the canonical type vocabulary documented in `ir.py`, with rich `details`), `ir.dependencies`, `ir.column_lineage` (best-effort), `ir.scheduling`, `ir.materialization`.
- 1-based line numbers in `Location`.
- dbt input may contain the model SQL and a `schema.yml` block separated by a line `--- schema.yml` (or detected YAML document). Put parsed YAML into `extras["schema_yml"]`, refs into `extras["refs"]`, sources into `extras["sources"]`.

### Rules

Rule modules define subclasses of `Rule` (see `app/rules/__init__.py`) decorated
with `@register`. Set `id`, `severity`, `category`, `formats`, `title`,
`description`; implement `check(self, pr: ParseResult) -> list[Issue]` using
`self.issue(message, line=..., fix_suggestion=..., fix_diff=...)`.
`fix_diff` is a unified diff string (`--- current` / `+++ optimized` with -/+ lines).

### Engines

```python
# app/engines/cost.py
def estimate_cost(pr: ParseResult, issues: list[Issue], *,
                  row_count: int = 10_000_000, daily_runs: int = 24,
                  warehouse: str = "snowflake") -> CostAnalysis: ...

# app/engines/lineage.py
def build_lineage(ir: IR) -> LineageGraph: ...

# app/engines/scoring.py
def compute_score(issues: list[Issue], pr: ParseResult) -> ProductionScore: ...
# Weights: efficiency .25, reliability .25, observability .20,
#          maintainability .15, security .15. All dims clamped 0–100.

# app/engines/impact.py
def simulate_issue_impact(issue: Issue, *, row_count: int, daily_runs: int,
                          warehouse: str, base_monthly_cost: float) -> ImpactResult: ...
def attach_impacts(issues: list[Issue], *, row_count: int, daily_runs: int,
                   warehouse: str, base_monthly_cost: float) -> list[ImpactResult]:
    # fills issue.impact in place AND returns the ImpactResult list

# app/engines/observability.py
def generate_tests(pr: ParseResult, issues: list[Issue]) -> GeneratedTests: ...

# app/engines/security.py
def scan_security(pr: ParseResult) -> SecurityReport: ...
def detect_pii_columns(pr: ParseResult) -> list[PIIColumn]: ...  # reused by security_rules
```

### LLM layer

```python
# app/llm/client.py
class LLMUnavailable(Exception): ...
def get_provider_status() -> ProviderInfo: ...          # cached ~30s, fast timeout (2s)
def list_providers() -> list[ProviderInfo]: ...          # status of all four providers
async def stream_completion(messages: list[dict]) -> AsyncIterator[str]: ...
# Providers via env: LLM_PROVIDER = ollama|gemini|groq|openrouter (see backend/.env.example)
# All use the OpenAI-compatible SDK (openai package, AsyncOpenAI).

# app/llm/prompts.py
SYSTEM_PROMPT: str
def build_task_messages(task: str, report: AnalysisReport, issue: Issue | None) -> list[dict]: ...
# Tasks: explain | issue | optimize | cost | observability
# The user message embeds compacted IR/report JSON — NEVER raw source code.

# app/llm/tasks.py
async def stream_task(task: str, report: AnalysisReport,
                      issue_id: str | None = None) -> AsyncIterator[str]: ...
# If provider unavailable: yield a useful deterministic fallback built from the
# report (template-based), prefixed with "[offline analysis] ", then return.
```

### Service + store + config

```python
# app/services/analyzer.py
def analyze_full(code: str, *, format: str = "auto", dialect: str | None = None,
                 row_count: int = 10_000_000, daily_runs: int = 24,
                 warehouse: str = "snowflake") -> tuple[AnalysisReport, ParseResult]: ...
def analyze(code: str, **kwargs) -> AnalysisReport: ...   # wrapper over analyze_full
# Orchestration: parse → run_rules → estimate_cost → attach_impacts →
# build_lineage → compute_score → generate_tests → scan_security →
# deterministic 1-3 sentence summary + optimizations list → AnalysisReport
# (set created_at = UTC ISO string, params, parser_warnings, ir).

# app/store.py  (in-memory, thread-safe, max 200 entries, FIFO eviction)
def save(report: AnalysisReport, parse_result: ParseResult | None = None) -> None: ...
def get(report_id: str) -> AnalysisReport | None: ...
def get_parse_result(report_id: str) -> ParseResult | None: ...
# /api/simulate/impact and /api/cost/estimate re-run estimate_cost +
# attach_impacts against the stored ParseResult with the new params.

# app/config.py  (loads backend/.env via python-dotenv at import)
class Settings: llm_provider, ollama_base_url, ollama_model, gemini_api_key,
                gemini_model, groq_api_key, groq_model, openrouter_api_key,
                openrouter_model, cors_origins: list[str]
settings = Settings()
```

### API (FastAPI in `app/api.py`, CORS for http://localhost:3000)

```
GET  /api/health            -> {"status": "ok", "provider": ProviderInfo}
GET  /api/providers         -> list[ProviderInfo]
POST /api/analyze           -> AnalysisReport            body: AnalyzeRequest (see types.ts)
POST /api/explain           -> text/event-stream         body: {analysis_id, task, issue_id?}
POST /api/explain/issue     -> alias of /api/explain with task="issue"
GET  /api/analyze/{id}/stream?task=explain -> text/event-stream (EventSource-compatible)
POST /api/simulate/impact   -> {"impacts": [ImpactResult], "cost": CostAnalysis}
                               body: {analysis_id, row_count, daily_runs, warehouse}
POST /api/cost/estimate     -> CostAnalysis              body: same as simulate
```

Errors: HTTP 400 with `{"detail": "<ParseError message>"}` for parse failures,
404 for unknown analysis_id, 422 left to FastAPI validation.

SSE wire protocol (both stream endpoints):
```
data: {"token": "text chunk"}\n\n      (repeated)
data: {"done": true}\n\n               (terminal)
data: {"error": "message"}\n\n         (on failure, then done)
```

---

## Frontend contracts

Stack: Next.js 16 App Router + React 19 + Tailwind v4 (`@theme` tokens in
globals.css) + TypeScript strict. Client components must start with
`"use client"`. Imports use the `@/` alias (e.g. `@/lib/types`,
`@/components/ui/card`). Installed libs: `@monaco-editor/react`,
`@xyflow/react`, `recharts`, `lucide-react`, `clsx`, `tailwind-merge`.

### Component ownership & props

```tsx
// app/page.tsx (owner: shell) — composes:
//   <AnalysisProvider> → <TopBar /> + split pane: <EditorPanel /> | <AnalysisPanel />
// components/top-bar.tsx       (shell)  — logo, detected format chip, provider status dot, Analyze button (uses useAnalysis)
// components/editor-panel.tsx  (shell)  — Monaco editor + sample picker; props: none (uses useAnalysis)
// components/analysis-panel.tsx (tabs-a) — tab bar (TAB_IDS from lib/types) + renders active tab; empty/loading/error states; props: none
// components/tabs/score-tab.tsx        (tabs-a) — props: none (useAnalysis)
// components/tabs/issues-tab.tsx       (tabs-a) — props: none
// components/tabs/lineage-tab.tsx      (tabs-b) — props: none
// components/tabs/cost-tab.tsx         (tabs-b) — props: none
// components/tabs/impact-tab.tsx       (tabs-c) — props: none
// components/tabs/observability-tab.tsx (tabs-c) — props: none
// components/tabs/security-tab.tsx     (tabs-c) — props: none
```

Every tab component: `"use client"`, default export a named function component,
reads `report` from `useAnalysis()`, renders a friendly empty state when
`report === null`.

### UI primitives (owner: design) — `components/ui/*.tsx`

```tsx
// button.tsx:  <Button variant="primary"|"outline"|"ghost" size="sm"|"md" />  (extends button props)
// card.tsx:    <Card>, <CardHeader>, <CardTitle>, <CardContent>  — paper card w/ block shadow
// badge.tsx:   <Badge tone="terra"|"ochre"|"frost"|"sage"|"ink" />
// severity-badge.tsx: <SeverityBadge severity={Severity} />  (CRITICAL→terra, WARNING→ochre, INFO→frost)
// tooltip.tsx: <Tooltip content={ReactNode}>{trigger}</Tooltip>  (pure CSS/React, no lib)
// select.tsx:  <Select value onChange options={{value,label}[]} label? />  (styled native select)
// slider.tsx:  <Slider value onChange min max step label? format?=(v)=>string />  (styled native range)
// progress.tsx: <Meter value max tone? label? />  — chunky rounded meter bar
// code-block.tsx: <CodeBlock code language? title? /> — mono block w/ copy button; renders unified diffs with +/- line tinting
// stream-text.tsx: <StreamText text streaming /> — renders streamed markdown-ish text with blinking caret
// lib/utils.ts: export function cn(...inputs: ClassValue[]): string  (clsx + tailwind-merge)
// components/memphis.tsx: <Squiggle/>, <DotGrid/>, <ShapeBurst/>, <PaperTexture/> decorative SVGs
```

### Design system (owner: design defines it in globals.css; everyone uses it)

Editorial, warm, paper-like. Light theme default. Rounded corners everywhere
(cards `rounded-2xl`, chips `rounded-full`). Memphis-inspired accents: offset
block shadows, dotted grids, squiggle dividers, bold 2px ink borders. Palette
adapts Snowflake cyan + Redis red onto earthy paper tones.

Tailwind v4 tokens (defined via `@theme` in `app/globals.css`) — use these
exact names, e.g. `bg-paper`, `text-ink`, `border-line`, `bg-terra`:

```
--color-paper:   #F6F0E4   page background (warm cream)
--color-paper2:  #FDFAF2   card surface
--color-paper3:  #EDE4D0   inset / wells / code bg
--color-ink:     #2E2620   primary text (warm near-black)
--color-inksoft: #6B5D4F   secondary text
--color-line:    #D8CCB8   borders / dividers
--color-terra:   #D2402E   Redis-derived terracotta (CRITICAL, danger)
--color-frost:   #1F8FB8   Snowflake-derived cyan, earthed (INFO, links, accents)
--color-ochre:   #D99A2B   warning / WARNING severity
--color-sage:    #6F8F6A   success / good scores
--color-plum:    #7D5BA6   playful Memphis accent
--font-display:  Fraunces (serif, editorial headlines)
--font-sans:     Inter
--font-mono:     JetBrains Mono
```

Utility classes design must provide in globals.css: `.shadow-block`
(4px 4px 0 0 ink), `.shadow-block-sm` (2px 2px 0 0 ink), `.paper-texture`
(SVG turbulence noise overlay), `.font-display`. Score coloring: ≥80 sage,
50–79 ochre, <50 terra. Severity: CRITICAL terra / WARNING ochre / INFO frost.

Monaco theme (owner: shell): warm paper theme — bg `#FDFAF2`, keywords
`#C13E2C`, strings `#6F8F6A`, numbers `#B8860B`, comments `#9A8B76` italic,
default text `#2E2620`.

---

## Code style

- Backend: Python 3.11+ type hints, no TODO/placeholder/`pass` stubs, no prints
  (use logging), every public function has a docstring. Use sqlglot for ALL SQL
  parsing — never regex over SQL (regex is fine for fingerprinting/PII names).
- Frontend: TypeScript strict (no `any` unless unavoidable), accessible
  (labels, aria), no inline hex colors — use tokens.
- Everything must work offline (no external fonts/CDNs at runtime besides
  next/font; LLM providers degrade gracefully to deterministic fallbacks).
