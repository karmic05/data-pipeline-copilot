"""Analyzer service contract tests.

``services.analyzer.analyze`` must assemble a fully-populated
``AnalysisReport``: bounded production score + dimensions, internally-consistent
non-negative cost, a non-empty lineage graph, parseable generated dbt tests, the
detected PII email column, a non-empty summary, and a meaningful issue count.
"""
from __future__ import annotations

import pytest
import yaml

from app.schemas.report import AnalysisReport
from app.services import analyzer


@pytest.fixture(scope="module")
def report(snowflake_src: str) -> AnalysisReport:
    """A full analysis of the Snowflake example, computed once per module."""
    return analyzer.analyze(snowflake_src)


def test_returns_analysis_report(report: AnalysisReport) -> None:
    """analyze() returns an AnalysisReport for the SQL format."""
    assert isinstance(report, AnalysisReport)
    assert report.format == "sql"


def test_production_score_bounded(report: AnalysisReport) -> None:
    """Total and all five dimension scores stay within [0, 100]."""
    score = report.production_score
    assert 0 <= score.total <= 100
    for dimension in (
        score.efficiency,
        score.reliability,
        score.observability,
        score.maintainability,
        score.security,
    ):
        assert 0 <= dimension <= 100


def test_cost_analysis_consistent(report: AnalysisReport) -> None:
    """Current cost >= optimized cost >= 0."""
    cost = report.cost_analysis
    assert cost.monthly_usd_current >= cost.monthly_usd_optimized >= 0


def test_lineage_has_nodes(report: AnalysisReport) -> None:
    """The lineage graph is non-empty for a multi-table query."""
    assert len(report.lineage.nodes) > 0


def test_generated_dbt_yaml_parses(report: AnalysisReport) -> None:
    """The generated dbt schema YAML is non-empty and safe_load-parseable."""
    dbt_yaml = report.generated_tests.dbt_yaml
    assert dbt_yaml.strip()
    parsed = yaml.safe_load(dbt_yaml)
    assert parsed is not None


def test_security_detects_email_pii(report: AnalysisReport) -> None:
    """The customer_email PII column is detected in the security report."""
    pii_columns = {p.column.lower() for p in report.security.pii_columns}
    assert "customer_email" in pii_columns


def test_summary_and_issue_count(report: AnalysisReport) -> None:
    """The report has a non-empty summary and at least five issues."""
    assert report.summary.strip()
    assert len(report.issues) >= 5
