"""Rule engine contract tests.

``rules.run_rules(parse_result)`` must surface the expected anti-pattern rule
ids, return issues sorted by severity (no CRITICAL after a WARNING/INFO), and
emit only well-formed issues (non-empty message, allowed category).
"""
from __future__ import annotations

from typing import get_args

import pytest

from app import parsers
from app.rules import run_rules
from app.schemas.report import SEVERITY_ORDER, IssueCategory

from .conftest import EXAMPLE_FORMATS, read_example

#: The closed set of issue categories the rule engine may emit.
ALLOWED_CATEGORIES = set(get_args(IssueCategory))


def _rule_ids(name: str):
    """Run the rule engine over an example and return its (issues, id set)."""
    issues = run_rules(parsers.parse(read_example(name)))
    return issues, {issue.rule for issue in issues}


def test_snowflake_flags_select_star_and_cartesian_join() -> None:
    """The Snowflake example must surface SELECT_STAR and CARTESIAN_JOIN."""
    _issues, ids = _rule_ids("snowflake_orders.sql")
    assert "SELECT_STAR" in ids
    assert "CARTESIAN_JOIN" in ids


def test_airflow_flags_no_retries() -> None:
    """The Airflow example must surface the NO_RETRIES reliability rule."""
    _issues, ids = _rule_ids("airflow_etl.py")
    assert "NO_RETRIES" in ids


@pytest.mark.parametrize("name", sorted(EXAMPLE_FORMATS))
def test_issues_are_severity_sorted(name: str) -> None:
    """No CRITICAL issue ever follows a WARNING/INFO in the returned order."""
    issues = run_rules(parsers.parse(read_example(name)))
    ranks = [SEVERITY_ORDER[issue.severity] for issue in issues]
    assert ranks == sorted(ranks)


@pytest.mark.parametrize("name", sorted(EXAMPLE_FORMATS))
def test_every_issue_is_well_formed(name: str) -> None:
    """Each issue has a non-empty message and an allowed category."""
    issues = run_rules(parsers.parse(read_example(name)))
    assert issues, f"expected at least one issue for {name}"
    for issue in issues:
        assert issue.message.strip(), f"empty message from rule {issue.rule}"
        assert issue.category in ALLOWED_CATEGORIES
        assert issue.severity in SEVERITY_ORDER
        assert issue.rule  # rule id is always populated
