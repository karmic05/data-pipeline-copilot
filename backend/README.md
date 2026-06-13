# Data Pipeline Copilot — Backend

A deterministic static-analysis engine for data pipelines. Paste SQL, an Airflow
DAG, a dbt model, a PySpark job, a Prefect/Dagster flow, Flink SQL, a Kafka
Streams topology, or a Great Expectations suite and the backend parses it into a
unified IR, runs 85+ deterministic rules, and produces a full `AnalysisReport`:
production-readiness score, cost projection, lineage graph, simulated incident
impact, generated tests, and a PII/security scan.

The pipeline is **deterministic first**: parser → IR → rule engine (source of
truth) → cost/lineage/score/impact/observability/security engines →
`AnalysisReport`. An optional LLM layer only *explains and suggests* over the
report (never raw code) and streams over SSE. With no LLM configured it degrades
to a deterministic offline fallback, so everything works offline.

---

## Setup

Requires **Python 3.11+** (developed on 3.14). From this `backend/` directory:

```bash
# 1. Create and activate a virtual environment
python -m venv venv
source venv/Scripts/activate      # Windows (Git Bash)
# source venv/bin/activate        # macOS / Linux

# 2. Install dependencies
pip install -r requirements.txt

# 3. Copy the example env file and edit it for your LLM provider
cp .env.example .env

# 4. Run the API (http://localhost:8000, docs at /docs)
uvicorn main:app --reload --port 8000
```

The server is fully usable with the default `.env` even if no LLM is reachable —
analysis is deterministic and the explain endpoints fall back to a templated,
report-derived response.

---

## LLM providers

Pick **one** provider via `LLM_PROVIDER` in `.env`. All providers are accessed
through the OpenAI-compatible SDK. If the configured provider is unreachable, the
explain/optimize streams emit a deterministic `[offline analysis …]` fallback
built from the report.

| Provider     | `LLM_PROVIDER` | Env keys                              | Default model                              | Free-tier notes |
|--------------|----------------|---------------------------------------|--------------------------------------------|-----------------|
| **Ollama**   | `ollama`       | `OLLAMA_BASE_URL`, `OLLAMA_MODEL`     | `qwen2.5-coder:14b`                        | Local, free, private — recommended for dev. Run `ollama pull qwen2.5-coder:14b`. No key needed. |
| **Gemini**   | `gemini`       | `GEMINI_API_KEY`, `GEMINI_MODEL`      | `gemini-2.0-flash`                         | Generous free tier; key from [aistudio.google.com](https://aistudio.google.com). |
| **Groq**     | `groq`         | `GROQ_API_KEY`, `GROQ_MODEL`          | `llama-3.3-70b-versatile`                  | Fast free tier with rate limits; key from [console.groq.com](https://console.groq.com). |
| **OpenRouter** | `openrouter` | `OPENROUTER_API_KEY`, `OPENROUTER_MODEL` | `meta-llama/llama-3.3-70b-instruct:free` | Free `:free` models available; key from [openrouter.ai](https://openrouter.ai). |

`CORS_ORIGINS` (comma-separated) controls allowed browser origins and defaults to
`http://localhost:3000` for the bundled frontend.

---

## Endpoint reference

All routes are prefixed with `/api`. Request/response shapes mirror
`frontend/lib/types.ts`; see `CONTRACTS.md` for the authoritative schemas.

| Method & path | Body | Returns |
|---------------|------|---------|
| `GET  /api/health` | — | `{ "status": "ok", "provider": ProviderInfo }` |
| `GET  /api/providers` | — | `ProviderInfo[]` (status of all four providers) |
| `POST /api/analyze` | `{ code, format?, dialect?, row_count?, daily_runs?, warehouse? }` | `AnalysisReport` (**400** on parse failure) |
| `POST /api/explain` | `{ analysis_id, task, issue_id? }` | `text/event-stream` SSE tokens |
| `POST /api/explain/issue` | `{ analysis_id, issue_id? }` | SSE; alias of `/api/explain` with `task="issue"` |
| `GET  /api/analyze/{id}/stream?task=explain` | — | `text/event-stream` (EventSource-compatible) |
| `POST /api/simulate/impact` | `{ analysis_id, row_count, daily_runs, warehouse }` | `{ "impacts": ImpactResult[], "cost": CostAnalysis }` |
| `POST /api/cost/estimate` | same as simulate | `CostAnalysis` |

`format` accepts `auto` (default) plus any `PipelineFormat`
(`sql`, `airflow`, `dbt`, `spark`, `prefect`, `flink`, `kafka`,
`great_expectations`). `task` is one of `explain | issue | optimize | cost |
observability`.

**SSE wire protocol** (both stream endpoints):

```
data: {"token": "text chunk"}\n\n      (repeated)
data: {"error": "message"}\n\n         (on failure, then done)
data: {"done": true}\n\n               (terminal, always emitted)
```

**Errors:** `400` with `{"detail": "<message>"}` on parse failure, `404` for an
unknown `analysis_id`, `422` for request-validation errors.

---

## Example pipelines

`examples/` holds self-contained, intentionally-flawed pipelines used by the test
suite and the frontend sample picker:

- `snowflake_orders.sql` — `SELECT *`, comma/cross join, `NOT IN` on a nullable
  column, leading-wildcard `LIKE`, correlated scalar subquery, hardcoded date,
  missing partition filter, and email/phone PII reaching the output.
- `airflow_etl.py` — 8-task daily DAG: `retries=0`, no `catchup`, a poke-mode S3
  sensor, an XCom carrying a DataFrame, three heavy SparkSubmit tasks chained
  sequentially, and no SLA/owner/`on_failure_callback`.
- `dbt_orders.sql` — incremental model with no strategy/`is_incremental()` guard,
  plus a `schema.yml` block with an untested source and missing descriptions.
- `spark_sessions.py` — structured streaming with no `withWatermark`, no
  `checkpointLocation`, a `crossJoin`, a `.collect()`, and a `.coalesce(1).write`.
- `flink_clicks.sql` — Kafka source table with no `WATERMARK`, a 2-hour tumbling
  window, and an `ORDER BY` over an unbounded stream.

---

## Running the tests

The suite (`tests/`) asserts contract-level guarantees only — detection, parsing,
rule ids/sorting, the assembled report, and the API surface.

```bash
# from this backend/ directory
venv/Scripts/python -m pytest -q          # Windows
# venv/bin/python -m pytest -q            # macOS / Linux
```

`pytest.ini` sets `pythonpath = .` and `testpaths = tests`, so no extra
configuration is needed. The API tests use Starlette's `TestClient` and do not
require a running server or a live LLM (the offline fallback is exercised).

---

## Example curl

```bash
# 1. Analyze a pipeline (returns an AnalysisReport with an id)
curl -s -X POST http://localhost:8000/api/analyze \
  -H 'Content-Type: application/json' \
  -d '{"code": "CREATE OR REPLACE TABLE m AS SELECT * FROM a, b WHERE a.d >= '\''2023-01-01'\''", "format": "auto"}'

# 2. Stream an explanation of that analysis (SSE)
curl -N -X POST http://localhost:8000/api/explain \
  -H 'Content-Type: application/json' \
  -d '{"analysis_id": "<id-from-step-1>", "task": "explain"}'

# 3. Re-simulate cost + incident impact at a new scale
curl -s -X POST http://localhost:8000/api/simulate/impact \
  -H 'Content-Type: application/json' \
  -d '{"analysis_id": "<id-from-step-1>", "row_count": 500000000, "daily_runs": 2, "warehouse": "snowflake"}'
```
