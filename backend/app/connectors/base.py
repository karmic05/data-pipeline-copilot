"""Read-only connector contract for Phase-2 live warehouse enrichment.

A :class:`Connector` is a *read-only* lens onto a live warehouse used to enrich
(never to mutate) analysis: list tables, fetch real column schemas, and read
recent query-history cost. Concrete implementations (Phase 2) wrap
Snowflake ``ACCOUNT_USAGE`` / ``INFORMATION_SCHEMA``, BigQuery
``INFORMATION_SCHEMA`` + ``JOBS``, and Postgres ``information_schema`` /
``pg_stat_statements``. Bridging this with the agentic workflow lets the agent
optionally ground its findings in real schemas and real billing.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import List, Optional


class ConnectorUnavailable(Exception):
    """Raised when a live connection cannot be established or is not configured."""


@dataclass
class ColumnInfo:
    name: str
    data_type: str
    nullable: bool = True
    is_partition_key: bool = False


@dataclass
class TableSchema:
    name: str
    schema_name: Optional[str] = None
    database: Optional[str] = None
    columns: List[ColumnInfo] = field(default_factory=list)
    estimated_row_count: Optional[int] = None
    partition_columns: List[str] = field(default_factory=list)


@dataclass
class QueryStat:
    """A single observed historical execution, used to calibrate real cost."""

    query_id: str
    bytes_scanned: Optional[int] = None
    credits_used: Optional[float] = None
    cost_usd: Optional[float] = None
    elapsed_ms: Optional[int] = None
    rows_produced: Optional[int] = None


class Connector(abc.ABC):
    """Read-only interface a live warehouse connector must implement (Phase 2).

    Implementations MUST be read-only: they introspect metadata and query
    history, never execute user DML/DDL. ``warehouse`` identifies the pricing
    model to apply (snowflake | bigquery | redshift | databricks | postgres).
    """

    warehouse: str = "snowflake"

    @abc.abstractmethod
    def test_connection(self) -> bool:
        """Return True if a live, read-only connection is reachable."""

    @abc.abstractmethod
    def list_tables(self, schema: Optional[str] = None) -> List[str]:
        """List fully-qualified table names visible to the connection."""

    @abc.abstractmethod
    def get_schema(self, table: str) -> TableSchema:
        """Return the real column schema (resolves ``SELECT *`` / ambiguity)."""

    @abc.abstractmethod
    def query_history(
        self, *, table: Optional[str] = None, limit: int = 200
    ) -> List[QueryStat]:
        """Return recent executions to calibrate cost against real billing."""

    def close(self) -> None:  # optional override
        """Release any underlying resources."""
        return None
