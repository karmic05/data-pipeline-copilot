"""In-process DuckDB connector — the always-works, no-credentials demo.

DuckDB is an embedded SQL engine (no server, no credentials), which makes it the
perfect *demo* connector for Data Pipeline Copilot: it proves the whole live-DB
flow (test connection -> list tables -> introspect real schemas -> profile a
query with ``EXPLAIN ANALYZE`` cardinalities) entirely offline and is fully
testable in CI.

When opened in-memory (the demo default) it seeds a small but realistic
warehouse whose ``raw`` / ``analytics`` schemas line up with the app's default
sample pipelines, so the live database mirrors the analyzed code. All profiling
is strictly read-only: only ``EXPLAIN ANALYZE`` of a read query is ever run, and
anything that looks like DDL/DML is refused.
"""
from __future__ import annotations

import re
import uuid
from typing import List, Optional

from app.connectors.base import (
    ColumnInfo,
    Connector,
    ConnectorUnavailable,
    QueryStat,
    TableSchema,
)

# Schemas DuckDB exposes that are not user data — never list/introspect these.
_SYSTEM_SCHEMAS = {"information_schema", "pg_catalog", "main"}

# Leading keywords that indicate a mutating / DDL statement we must never profile.
_MUTATING = (
    "create",
    "insert",
    "update",
    "delete",
    "drop",
    "merge",
    "alter",
    "truncate",
    "replace",
    "attach",
    "detach",
    "copy",
    "call",
    "pragma",
    "set",
    "vacuum",
    "load",
    "install",
)

# Rough bytes-per-row estimate when the plan gives no scanned-bytes signal.
_BYTES_PER_ROW = 200


class DuckDBConnector(Connector):
    """Read-only DuckDB connector + bundled demo warehouse.

    With an in-memory database (``config.database`` empty / ``:memory:``) the
    constructor seeds a deterministic demo warehouse. With a file path it simply
    opens that database read-only-in-spirit (we only ever run ``EXPLAIN ANALYZE``
    and metadata queries).
    """

    kind = "duckdb"
    warehouse = "duckdb"
    requires_credentials = False

    def __init__(self, config) -> None:
        import duckdb  # local import so registry availability check is cheap

        self._config = config
        database = getattr(config, "database", None) or ":memory:"
        options = dict(getattr(config, "options", None) or {})

        self._in_memory = database == ":memory:"
        self._con = duckdb.connect(database=database)
        self._history: List[QueryStat] = []

        # Seed only an in-memory demo DB; never mutate a user-supplied file.
        seed = options.get("seed", "").lower() not in ("false", "0", "no")
        if self._in_memory and seed:
            self._seed_demo()

    # ------------------------------------------------------------------ seed
    def _seed_demo(self) -> None:
        """Create the demo ``raw`` / ``analytics`` warehouse, deterministically.

        Tables and columns mirror the app's default sample pipelines so the live
        database lines up with the analyzed SQL/Spark code. Rows are generated
        with ``range(...)`` so ``EXPLAIN ANALYZE`` sees real cardinalities.
        """
        con = self._con
        con.execute("CREATE SCHEMA IF NOT EXISTS raw")
        con.execute("CREATE SCHEMA IF NOT EXISTS analytics")

        # raw.customers -- 60 customers
        con.execute(
            """
            CREATE TABLE raw.customers (
                customer_id   BIGINT,
                customer_email VARCHAR,
                customer_phone VARCHAR,
                city          VARCHAR,
                signup_date   DATE
            )
            """
        )
        con.execute(
            """
            INSERT INTO raw.customers
            SELECT
                i AS customer_id,
                'customer' || i || '@'
                    || (['gmail.com','outlook.com','example.org'][(i % 3) + 1]) AS customer_email,
                '+1-555-' || lpad(((i * 7) % 10000)::VARCHAR, 4, '0') AS customer_phone,
                (['Austin','Denver','Seattle','Chicago','Boston','Reno'][(i % 6) + 1]) AS city,
                DATE '2022-01-01' + (i * 3)::INTEGER AS signup_date
            FROM range(60) t(i)
            """
        )

        # raw.orders -- 80 orders referencing the 60 customers
        con.execute(
            """
            CREATE TABLE raw.orders (
                order_id    BIGINT,
                customer_id BIGINT,
                status      VARCHAR,
                amount      DECIMAL(12, 2),
                created_at  TIMESTAMP
            )
            """
        )
        con.execute(
            """
            INSERT INTO raw.orders
            SELECT
                i AS order_id,
                (i % 60) AS customer_id,
                (['pending','paid','shipped','cancelled','refunded'][(i % 5) + 1]) AS status,
                ((i * 13) % 500 + 10)::DECIMAL(12, 2) AS amount,
                TIMESTAMP '2023-01-01 00:00:00' + (i * INTERVAL 7 HOUR) AS created_at
            FROM range(80) t(i)
            """
        )

        # raw.payments -- 70 payments referencing orders
        con.execute(
            """
            CREATE TABLE raw.payments (
                payment_id  BIGINT,
                order_id    BIGINT,
                customer_id BIGINT,
                amount      DECIMAL(12, 2),
                currency    VARCHAR,
                paid_at     TIMESTAMP
            )
            """
        )
        con.execute(
            """
            INSERT INTO raw.payments
            SELECT
                i AS payment_id,
                (i % 80) AS order_id,
                ((i % 80) % 60) AS customer_id,
                ((i * 11) % 500 + 10)::DECIMAL(12, 2) AS amount,
                (['USD','USD','EUR','GBP'][(i % 4) + 1]) AS currency,
                TIMESTAMP '2023-01-02 00:00:00' + (i * INTERVAL 9 HOUR) AS paid_at
            FROM range(70) t(i)
            """
        )

        # raw.addresses -- one address per customer (60)
        con.execute(
            """
            CREATE TABLE raw.addresses (
                address_id   BIGINT,
                customer_id  BIGINT,
                address_text VARCHAR,
                postal_code  VARCHAR
            )
            """
        )
        con.execute(
            """
            INSERT INTO raw.addresses
            SELECT
                i AS address_id,
                i AS customer_id,
                (100 + i) || ' Main St, Unit ' || (i % 25) AS address_text,
                lpad(((i * 97) % 100000)::VARCHAR, 5, '0') AS postal_code
            FROM range(60) t(i)
            """
        )

        # raw.excluded_statuses -- small lookup table (anti-join target)
        con.execute(
            """
            CREATE TABLE raw.excluded_statuses (
                status VARCHAR
            )
            """
        )
        con.execute(
            """
            INSERT INTO raw.excluded_statuses
            SELECT (['cancelled','refunded','fraud'][i + 1]) AS status
            FROM range(3) t(i)
            """
        )

        # analytics.enriched_orders -- the mart the sample pipeline builds (75 rows)
        con.execute(
            """
            CREATE TABLE analytics.enriched_orders (
                order_id       BIGINT,
                created_at     TIMESTAMP,
                customer_email VARCHAR,
                amount         DECIMAL(12, 2)
            )
            """
        )
        con.execute(
            """
            INSERT INTO analytics.enriched_orders
            SELECT
                o.order_id,
                o.created_at,
                c.customer_email,
                o.amount
            FROM raw.orders o
            JOIN raw.customers c ON o.customer_id = c.customer_id
            WHERE o.status NOT IN (SELECT status FROM raw.excluded_statuses)
            """
        )

    # ------------------------------------------------------------ connection
    def test_connection(self) -> bool:
        try:
            result = self._con.execute("SELECT 1").fetchone()
        except Exception as exc:  # pragma: no cover - defensive
            raise ConnectorUnavailable(f"DuckDB connection failed: {exc}") from exc
        return bool(result and result[0] == 1)

    # --------------------------------------------------------------- tables
    def list_tables(self, schema: Optional[str] = None) -> List[str]:
        sql = (
            "SELECT table_schema, table_name FROM information_schema.tables "
            "WHERE table_schema NOT IN ('information_schema', 'pg_catalog') "
        )
        params: list = []
        if schema:
            sql += "AND table_schema = ? "
            params.append(schema)
        sql += "ORDER BY table_schema, table_name"
        try:
            rows = self._con.execute(sql, params).fetchall()
        except Exception as exc:
            raise ConnectorUnavailable(f"Failed to list tables: {exc}") from exc
        return [f"{s}.{t}" for (s, t) in rows]

    # --------------------------------------------------------------- schema
    def get_schema(self, table: str) -> TableSchema:
        if "." in table:
            schema_name, table_name = table.split(".", 1)
        else:
            schema_name, table_name = "main", table

        try:
            rows = self._con.execute(
                """
                SELECT column_name, data_type, is_nullable
                FROM information_schema.columns
                WHERE table_schema = ? AND table_name = ?
                ORDER BY ordinal_position
                """,
                [schema_name, table_name],
            ).fetchall()
        except Exception as exc:
            raise ConnectorUnavailable(
                f"Failed to read schema for {table!r}: {exc}"
            ) from exc

        if not rows:
            raise ConnectorUnavailable(f"Table {table!r} does not exist")

        columns = [
            ColumnInfo(
                name=name,
                data_type=str(data_type),
                nullable=str(is_nullable).upper() != "NO",
            )
            for (name, data_type, is_nullable) in rows
        ]

        estimated_row_count: Optional[int] = None
        try:
            qualified = f'"{schema_name}"."{table_name}"'
            count_row = self._con.execute(
                f"SELECT count(*) FROM {qualified}"
            ).fetchone()
            if count_row is not None:
                estimated_row_count = int(count_row[0])
        except Exception:  # pragma: no cover - count is best-effort
            estimated_row_count = None

        return TableSchema(
            name=table_name,
            schema_name=schema_name,
            columns=columns,
            estimated_row_count=estimated_row_count,
        )

    # -------------------------------------------------------------- profile
    def profile_query(self, sql: str) -> QueryStat:
        if self._looks_mutating(sql):
            raise ConnectorUnavailable(
                "Refusing to profile a mutating statement; "
                "only read queries can be profiled."
            )

        try:
            result = self._con.execute("EXPLAIN ANALYZE " + sql).fetchall()
        except Exception as exc:
            raise ConnectorUnavailable(f"Query profiling failed: {exc}") from exc

        plan = result[0][1] if result and len(result[0]) > 1 else ""
        elapsed_ms = self._parse_elapsed_ms(plan)
        rows_produced = self._parse_top_cardinality(plan)
        bytes_scanned = (
            rows_produced * _BYTES_PER_ROW if rows_produced is not None else None
        )

        stat = QueryStat(
            query_id=uuid.uuid4().hex,
            rows_produced=rows_produced,
            elapsed_ms=elapsed_ms,
            bytes_scanned=bytes_scanned,
        )
        self._history.append(stat)
        return stat

    # -------------------------------------------------------------- history
    def query_history(
        self, *, table: Optional[str] = None, limit: int = 200
    ) -> List[QueryStat]:
        items = list(reversed(self._history))
        if limit is not None and limit >= 0:
            items = items[:limit]
        return items

    # ----------------------------------------------------------------- close
    def close(self) -> None:
        try:
            self._con.close()
        except Exception:  # pragma: no cover - defensive
            pass

    # --------------------------------------------------------------- helpers
    @staticmethod
    def _looks_mutating(sql: str) -> bool:
        """True if ``sql`` begins with a DDL/DML keyword after stripping noise."""
        text = sql or ""
        # Strip /* ... */ block comments and -- line comments.
        text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
        text = re.sub(r"--[^\n]*", " ", text)
        text = text.strip()
        # Skip leading parens (e.g. "(SELECT ...) UNION ...").
        text = text.lstrip("( \t\r\n")
        if not text:
            # Nothing executable -> treat as unsafe to profile.
            return True
        first = re.match(r"[a-zA-Z_]+", text)
        if not first:
            return True
        return first.group(0).lower() in _MUTATING

    @staticmethod
    def _parse_elapsed_ms(plan: str) -> Optional[int]:
        """Parse ``Total Time: 0.0033s`` from the analyzed plan -> milliseconds."""
        m = re.search(r"Total Time:\s*([0-9.]+)\s*s", plan)
        if not m:
            return None
        try:
            return int(round(float(m.group(1)) * 1000))
        except ValueError:  # pragma: no cover
            return None

    @staticmethod
    def _parse_top_cardinality(plan: str) -> Optional[int]:
        """Return the first meaningful operator cardinality in the plan.

        The plan renders each operator with an ``N rows`` line top-down. The
        outermost ``EXPLAIN_ANALYZE`` node reports ``0 rows``; the first non-zero
        count below it is the top real operator's produced-row cardinality. If
        every operator is zero (e.g. a query that truly returns nothing), fall
        back to 0.
        """
        counts = re.findall(r"([0-9,]+)\s+rows", plan)
        if not counts:
            return None
        parsed = [int(c.replace(",", "")) for c in counts]
        for value in parsed:
            if value > 0:
                return value
        return parsed[0]
