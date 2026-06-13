"""Deterministic rule engine framework.

Rule modules (sql_rules, airflow_rules, dbt_rules, streaming_rules,
security_rules) define :class:`Rule` subclasses and register them with the
``@register`` decorator. ``run_rules(parse_result)`` executes every rule whose
``formats`` set includes the IR's format and returns severity-sorted issues.

The rule engine is the source of truth — the LLM only explains and suggests.
"""
from __future__ import annotations

import hashlib
import importlib
import logging
from abc import ABC, abstractmethod
from typing import FrozenSet, List, Optional, Type

from app.schemas.ir import Location, ParseResult
from app.schemas.report import SEVERITY_ORDER, Issue, IssueCategory, Severity

logger = logging.getLogger(__name__)

ALL_FORMATS: FrozenSet[str] = frozenset(
    {"sql", "airflow", "dbt", "spark", "prefect", "flink", "kafka", "great_expectations"}
)


class Rule(ABC):
    """One deterministic check. Subclasses set class attributes and implement check()."""

    id: str = ""
    severity: Severity = "INFO"
    category: IssueCategory = "maintainability"
    formats: FrozenSet[str] = ALL_FORMATS
    title: str = ""
    description: str = ""

    @abstractmethod
    def check(self, pr: ParseResult) -> List[Issue]:
        """Return zero or more issues found in the parse result."""

    def issue(
        self,
        message: str,
        *,
        line: Optional[int] = None,
        col: int = 0,
        fix_suggestion: Optional[str] = None,
        fix_diff: Optional[str] = None,
    ) -> Issue:
        """Build an Issue pre-filled from this rule's metadata.

        The issue id is derived deterministically from ``(rule, line, col,
        message)`` rather than a random UUID, so re-analyzing identical source
        reproduces the exact same ids. This lets stateless deployments (where a
        follow-up request may re-run the analysis on a different worker) match
        the issue ids the client is already holding.
        """
        digest = hashlib.sha1(
            f"{self.id}|{line}|{col}|{message}".encode("utf-8")
        ).hexdigest()[:16]
        return Issue(
            id=digest,
            rule=self.id,
            severity=self.severity,
            category=self.category,
            title=self.title or self.id.replace("_", " ").title(),
            message=message,
            location=Location(line=line, col=col) if line is not None else None,
            fix_suggestion=fix_suggestion,
            fix_diff=fix_diff,
        )


REGISTRY: List[Rule] = []
_RULE_MODULES = [
    "app.rules.sql_rules",
    "app.rules.airflow_rules",
    "app.rules.dbt_rules",
    "app.rules.streaming_rules",
    "app.rules.security_rules",
]
_loaded = False


def register(rule_cls: Type[Rule]) -> Type[Rule]:
    """Class decorator: instantiate and add a rule to the registry."""
    REGISTRY.append(rule_cls())
    return rule_cls


def load_all() -> None:
    """Import every rule module exactly once (idempotent)."""
    global _loaded
    if _loaded:
        return
    for mod in _RULE_MODULES:
        try:
            importlib.import_module(mod)
        except Exception:  # pragma: no cover - a broken module must not kill the API
            logger.exception("Failed to load rule module %s", mod)
    _loaded = True


def run_rules(pr: ParseResult) -> List[Issue]:
    """Run every applicable rule against the parse result."""
    load_all()
    issues: List[Issue] = []
    for rule in REGISTRY:
        if pr.ir.format not in rule.formats:
            continue
        try:
            issues.extend(rule.check(pr))
        except Exception:  # pragma: no cover - one bad rule must not kill analysis
            logger.exception("Rule %s crashed", rule.id)
    issues.sort(
        key=lambda i: (
            SEVERITY_ORDER.get(i.severity, 9),
            i.location.line if i.location else 10**9,
        )
    )
    return issues
