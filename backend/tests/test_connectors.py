"""Real tests for the DuckDB demo connector and the connector registry.

DuckDB is in-process and needs no server, so unlike the credentialed connectors
these tests run unconditionally in CI. They exercise the full live-DB flow over
the seeded demo warehouse (test connection -> list -> introspect -> profile) and
assert the registry's availability/gating contract.
"""
from __future__ import annotations

import pytest

from app.connectors.base import ConnectorUnavailable, QueryStat, TableSchema
from app.connectors.duckdb_connector import DuckDBConnector
from app.connectors.registry import get_connector, list_connectors
from app.schemas.connectors import ConnectorConfig


@pytest.fixture()
def duck():
    """An in-memory DuckDB connector seeded with the demo warehouse."""
    connector = DuckDBConnector(ConnectorConfig(kind="duckdb"))
    try:
        yield connector
    finally:
        connector.close()


# --------------------------------------------------------------------- connector
def test_connector_class_contract():
    assert DuckDBConnector.kind == "duckdb"
    assert DuckDBConnector.warehouse == "duckdb"
    assert DuckDBConnector.requires_credentials is False


def test_test_connection_true(duck):
    assert duck.test_connection() is True


def test_list_tables_includes_seeded(duck):
    tables = duck.list_tables()
    assert "raw.orders" in tables
    assert "raw.customers" in tables
    # The other seeded tables should be present too.
    for expected in (
        "raw.payments",
        "raw.addresses",
        "raw.excluded_statuses",
        "analytics.enriched_orders",
    ):
        assert expected in tables
    # System schemas must be excluded.
    assert all(not t.startswith("information_schema.") for t in tables)


def test_list_tables_filter_by_schema(duck):
    raw_tables = duck.list_tables(schema="raw")
    assert raw_tables, "expected raw.* tables"
    assert all(t.startswith("raw.") for t in raw_tables)
    assert "raw.orders" in raw_tables
    assert "analytics.enriched_orders" not in raw_tables


def test_get_schema_customers(duck):
    schema = duck.get_schema("raw.customers")
    assert isinstance(schema, TableSchema)
    assert schema.name == "customers"
    assert schema.schema_name == "raw"
    assert schema.estimated_row_count is not None
    assert schema.estimated_row_count > 0

    by_name = {c.name: c for c in schema.columns}
    assert "customer_email" in by_name
    email = by_name["customer_email"]
    assert email.data_type  # non-empty data type
    assert isinstance(email.data_type, str)


def test_profile_query_returns_signals(duck):
    stat = duck.profile_query(
        "SELECT * FROM raw.orders o "
        "JOIN raw.customers c ON o.customer_id = c.customer_id"
    )
    assert isinstance(stat, QueryStat)
    # At least one cost signal must come back from EXPLAIN ANALYZE.
    assert stat.rows_produced is not None or stat.elapsed_ms is not None
    assert stat.query_id


def test_profile_query_records_history(duck):
    assert duck.query_history() == []
    duck.profile_query("SELECT count(*) FROM raw.orders")
    duck.profile_query("SELECT count(*) FROM raw.customers")
    history = duck.query_history()
    assert len(history) == 2
    assert all(isinstance(s, QueryStat) for s in history)
    # Most recent first, and the limit is honored.
    assert len(duck.query_history(limit=1)) == 1


@pytest.mark.parametrize(
    "bad_sql",
    [
        "CREATE TABLE x (a INT)",
        "DROP TABLE raw.orders",
        "INSERT INTO raw.orders VALUES (1, 1, 'x', 1.0, now())",
        "UPDATE raw.orders SET amount = 0",
        "DELETE FROM raw.orders",
        "  -- comment\n  DELETE FROM raw.orders",
        "/* block */ MERGE INTO raw.orders USING raw.orders ON true",
    ],
)
def test_profile_query_refuses_mutations(duck, bad_sql):
    with pytest.raises(ConnectorUnavailable):
        duck.profile_query(bad_sql)


def test_get_schema_missing_table_raises(duck):
    with pytest.raises(ConnectorUnavailable):
        duck.get_schema("raw.does_not_exist")


# ---------------------------------------------------------------------- registry
def test_list_connectors_gating():
    entries = {c["kind"]: c for c in list_connectors()}

    assert "duckdb" in entries
    duck_entry = entries["duckdb"]
    assert duck_entry["available"] is True
    assert duck_entry["enabled"] is True
    assert duck_entry["requires_credentials"] is False

    # Credentialed connectors are gated off by default.
    for kind in ("snowflake", "postgres"):
        assert kind in entries
        assert entries[kind]["enabled"] is False


def test_get_connector_duckdb_connects():
    connector = get_connector("duckdb", ConnectorConfig(kind="duckdb"))
    try:
        assert connector.test_connection() is True
    finally:
        connector.close()


def test_get_connector_postgres_disabled():
    with pytest.raises(ConnectorUnavailable):
        get_connector("postgres", ConnectorConfig(kind="postgres"))
