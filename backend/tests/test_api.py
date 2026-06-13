"""FastAPI endpoint contract tests via Starlette's TestClient.

Covers the request/response contracts in CONTRACTS.md: health + provider,
analyze (success and parse-failure), impact simulation, and the SSE explain
stream (the offline deterministic fallback is acceptable).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api import app


@pytest.fixture(scope="module")
def client() -> TestClient:
    """A TestClient bound to the FastAPI app."""
    return TestClient(app)


@pytest.fixture(scope="module")
def analysis_id(client: TestClient, snowflake_src: str) -> str:
    """Analyze the Snowflake example once and return its stored analysis id."""
    response = client.post(
        "/api/analyze", json={"code": snowflake_src, "format": "auto"}
    )
    assert response.status_code == 200
    return response.json()["id"]


def test_health_ok_with_provider(client: TestClient) -> None:
    """GET /api/health returns 200 with a provider block."""
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "provider" in body
    assert body["provider"]["provider"]  # provider name is non-empty


def test_analyze_returns_id(client: TestClient, snowflake_src: str) -> None:
    """POST /api/analyze on a valid pipeline returns 200 with an id."""
    response = client.post(
        "/api/analyze", json={"code": snowflake_src, "format": "auto"}
    )
    assert response.status_code == 200
    assert response.json()["id"]


def test_analyze_rejects_garbage(client: TestClient) -> None:
    """POST /api/analyze on undetectable input returns 400."""
    response = client.post("/api/analyze", json={"code": "!!!not a pipeline!!!"})
    assert response.status_code == 400


def test_simulate_impact_returns_impacts(client: TestClient, analysis_id: str) -> None:
    """POST /api/simulate/impact returns 200 with a non-empty impacts list."""
    response = client.post(
        "/api/simulate/impact",
        json={
            "analysis_id": analysis_id,
            "row_count": 500_000_000,
            "daily_runs": 2,
            "warehouse": "snowflake",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body["impacts"]) > 0


def test_explain_streams_sse(client: TestClient, analysis_id: str) -> None:
    """POST /api/explain returns 200 with SSE 'data:' chunks and a done event."""
    response = client.post(
        "/api/explain", json={"analysis_id": analysis_id, "task": "explain"}
    )
    assert response.status_code == 200
    body = response.text
    assert "data:" in body
    assert '"done": true' in body
