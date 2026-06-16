"""Live warehouse/database connectors — Phase 2.

Phase 1 of Data Pipeline Copilot is deliberately static, offline and
connection-free (that is the product's wedge). This package defines the
read-only connector interface that Phase 2 will implement to *optionally* enrich
analysis with live metadata — real table schemas (to resolve ``SELECT *`` and
ambiguous joins) and real query-history-based cost — without changing the
offline-by-default posture.

Nothing here opens a live connection yet; :class:`base.Connector` is the
contract concrete connectors (Postgres, Snowflake, BigQuery) will fulfill.
"""
from app.connectors.base import (  # noqa: F401
    ColumnInfo,
    Connector,
    ConnectorUnavailable,
    QueryStat,
    TableSchema,
)
