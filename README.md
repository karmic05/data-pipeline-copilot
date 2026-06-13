# Data Pipeline Copilot

A structured data-pipeline intelligence platform — think Datadog meets dbt Cloud
meets an AI code reviewer. Paste raw pipeline code (SQL, Airflow, dbt, PySpark,
Prefect/Dagster, Flink SQL, Kafka Streams, Great Expectations) and get a
deterministic, multi-dimensional analysis in seconds:

1. **Plain-English explanation** of what the pipeline does
2. **Issue detection** — 85+ deterministic rules across SQL, orchestration, streaming and security
3. **Optimization suggestions** with unified code diffs
4. **Cost estimation** — warehouse-aware dollar math (Snowflake, BigQuery, Redshift, Databricks)
5. **Column-level lineage** — interactive React Flow graph with Mermaid/JSON export
6. **Production Readiness Score** — 0–100 across 5 weighted dimensions, with a fix roadmap
7. **Production Impact Simulator** — per-issue failure modeling (latency, $/month, incidents/month)
8. **Observability gaps** + auto-generated dbt and Great Expectations test suites
9. **Security & PII scan** — PII column detection, secrets scanning, unmasked data flows

**Not a chatbot.** A deterministic parser layer produces a structured IR
(intermediate representation), an 85-rule engine runs against the IR, and the
LLM only ever sees the IR — never raw code. Every LLM output is tied to IR
nodes, and all outputs are schema-validated before rendering. Works fully
offline: with no LLM provider configured, explanations fall back to a
deterministic template engine and everything else is 100% local math.

## Architecture

```
frontend/   Next.js 16 + TypeScript + Tailwind v4 + Monaco + React Flow + Recharts
backend/    FastAPI + sqlglot + Pydantic v2 + stdlib ast + PyYAML
            app/parsers/   format auto-detection → unified IR
            app/rules/     85+ deterministic rules (source of truth)
            app/engines/   cost · lineage · scoring · impact · observability · security
            app/llm/       provider abstraction (Ollama / Gemini / Groq / OpenRouter) + SSE
```

See [CONTRACTS.md](CONTRACTS.md) for the full module-interface reference.

## Quick start

### 1. Backend (port 8000)

```powershell
cd backend
python -m venv venv
venv\Scripts\pip install -r requirements.txt
copy .env.example .env          # defaults to Ollama; works offline without it
venv\Scripts\python -m uvicorn main:app --reload --port 8000
```

### 2. Frontend (port 3000)

```powershell
cd frontend
npm install
npm run dev
```

Open http://localhost:3000, pick a sample (or paste your own pipeline), hit
**Analyze**.

### 3. LLM provider (optional — everything degrades gracefully without one)

| Provider   | Cost | Setup |
|------------|------|-------|
| **Ollama** (recommended) | free, local, private | install from https://ollama.com then `ollama pull qwen2.5-coder:14b` |
| Gemini     | free tier, no card | key from https://aistudio.google.com → `GEMINI_API_KEY` |
| Groq       | free tier, no card | key from https://console.groq.com → `GROQ_API_KEY` |
| OpenRouter | free models | key from https://openrouter.ai → `OPENROUTER_API_KEY` |

Set `LLM_PROVIDER` in `backend/.env`. The LLM receives only the structured IR —
your code never leaves the machine unless you choose a cloud provider.

## Tests

```powershell
cd backend
venv\Scripts\python -m pytest
```

Example pipelines (each deliberately broken in instructive ways) live in
`backend/examples/`.
