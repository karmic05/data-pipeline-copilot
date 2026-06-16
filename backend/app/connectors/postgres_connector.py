"""Read-only PostgreSQL / Redshift connector (Phase-2 live enrichment).

Wraps a real PostgreSQL-protocol warehouse (vanilla Postgres or Amazon Redshift,
which speaks the Postgres wire protocol) as a strictly read-only lens: it
introspects ``information_schema``, estimates row counts from ``pg_class``,
reads recent cost from ``pg_stat_statements`` (when the extension is present),
and profiles queries with ``EXPLAIN (FORMAT JSON)`` - it never executes the
user's query nor any DML/DDL.

The driver (``psycopg`` v3, falling back to ``psycopg2``) is *optional*: it is
imported lazily inside :meth:`_connect`, so this module imports cleanly with no
driver installed and raises :class:`ConnectorUnavailable` (never ``ImportError``)
only when an actual connection is attempted.
"""
from __future__ import annotations

import json
import uuid
from typing import List, Optional

from app.connectors.base import (
    ColumnInfo,
    Connector,
    ConnectorUnavailable,
    QueryStat,
    TableSchema,
)

# Schemas that hold engine internals, never user data - excluded from listing.
_SYSTEM_SCHEMAS = ("pg_catalog", "information_schema")

# Rough bytes-per-row estimate when the plan gives no scanned-bytes signal.
_BYTES_PER_ROW = 200


class PostgresConnector(Connector):
    """Read-only Postgres/Redshift connector.

    Lazily connects (the constructor only stores config). All access is
    introspection, ``EXPLAIN`` profiling, and history views; the underlying
    query is never run.
    """

    kind = "postgres"
    warehouse = "redshift"
    requires_credentials = True

    def __init__(self, config) -> None:
        # Do NOT connect here - connection is established lazily on first use.
        self._config = config

    # ------------------------------------------------------------ driver/conn
    def _connect(self):
        """Open a new live connection, importing the driver lazily.

        Tries ``psycopg`` (v3) first, then ``psycopg2``. Raises
        :class:`ConnectorUnavailable` (never ``ImportError``) when neither driver
        is installed or no DSN is configured.
        """
        dsn = getattr(self._config, "dsn", None)
        if not dsn:
            raise ConnectorUnavailable(
                "A DSN is required for the postgres connector "
                "(e.g. postgresql://user:pass@host:5432/db)."
            )

        try:
            import psycopg  # type: ignore

            driver = psycopg
        except ImportError:
            try:
                import psycopg2  # type: ignore

                driver = psycopg2
            except ImportError as exc:
                raise ConnectorUnavailable(
                    "Install psycopg (or psycopg2) to use the postgres connector."
                ) from exc

        try:
            return driver.connect(dsn)
        except Exception as exc:  # network / auth / config error
            raise ConnectorUnavailable(
                f"Postgres connection failed: {exc}"
            ) from exc

    # ------------------------------------------------------------ connection
    def test_connection(self) -> bool:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                row = cur.fetchone()
            return bool(row and row[0] == 1)
        except Exception as exc:
            raise ConnectorUnavailable(
                f"Postgres connection test failed: {exc}"
            ) from exc
        finally:
            self._safe_close(conn)

    # --------------------------------------------------------------- tables
    def list_tables(self, schema: Optional[str] = None) -> List[str]:
        sql = (
            "SELECT table_schema, table_name FROM information_schema.tables "
            "WHERE table_schema NOT IN (%s, %s)"
        )
        params: list = list(_SYSTEM_SCHEMAS)
        if schema:
            sql += " AND table_schema = %s"
            params.append(schema)
        sql += " ORDER BY table_schema, table_name"

        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
        except Exception as exc:
            raise ConnectorUnavailable(f"Failed to list tables: {exc}") from exc
        finally:
            self._safe_close(conn)
        return [f"{s}.{t}" for (s, t) in rows]

    # --------------------------------------------------------------- schema
    def get_schema(self, table: str) -> TableSchema:
        if "." in table:
            schema_name, table_name = table.split(".", 1)
        else:
            schema_name, table_name = "public", table

        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT column_name, data_type, is_nullable
                    FROM information_schema.columns
                    WHERE table_schema = %s AND table_name = %s
                    ORDER BY ordinal_position
                    """,
                    [schema_name, table_name],
                )
                rows = cur.fetchall()

                estimated_row_count: Optional[int] = None
                try:
                    cur.execute(
                        """
                        SELECT c.reltuples::bigint
                        FROM pg_class c
                        JOIN pg_namespace n ON n.oid = c.relnamespace
                        WHERE n.nspname = %s AND c.relname = %s
                        """,
                        [schema_name, table_name],
                    )
                    est = cur.fetchone()
                    if est is not None and est[0] is not None:
                        count = int(est[0])
                        # reltuples is -1 when never analyzed; treat as unknown.
                        estimated_row_count = count if count >= 0 else None
                except Exception:  # best-effort estimate
                    estimated_row_count = None
        except Exception as exc:
            raise ConnectorUnavailable(
                f"Failed to read schema for {table!r}: {exc}"
            ) from exc
        finally:
            self._safe_close(conn)

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

        return TableSchema(
            name=table_name,
            schema_name=schema_name,
            columns=columns,
            estimated_row_count=estimated_row_count,
        )

    # -------------------------------------------------------------- history
    def query_history(
        self, *, table: Optional[str] = None, limit: int = 200
    ) -> List[QueryStat]:
        """Read recent executions from ``pg_stat_statements``.

        Returns ``[]`` when the driver/extension is unavailable (the common
        case) - history is best-effort enrichment, never fatal.
        """
        try:
            conn = self._connect()
        except ConnectorUnavailable:
            return []
        try:
            with conn.cursor() as cur:
                try:
                    cur.execute(
                        """
                        SELECT query, calls, total_exec_time, rows
                        FROM pg_stat_statements
                        ORDER BY total_exec_time DESC
                        LIMIT %s
                        """,
                        [int(limit)],
                    )
                    rows = cur.fetchall()
                except Exception:
                    # Extension not present / not accessible -> no history.
                    return []
        except Exception:
            return []
        finally:
            self._safe_close(conn)

        out: List[QueryStat] = []
        for query, calls, total_exec_time, n_rows in rows:
            elapsed_ms: Optional[int] = None
            if total_exec_time is not None:
                try:
                    elapsed_ms = int(round(float(total_exec_time)))
                except (TypeError, ValueError):
                    elapsed_ms = None
            out.append(
                QueryStat(
                    query_id=uuid.uuid4().hex,
                    rows_produced=int(n_rows) if n_rows is not None else None,
                    elapsed_ms=elapsed_ms,
                )
            )
        return out

    # -------------------------------------------------------------- profile
    def profile_query(self, sql: str) -> QueryStat:
        """Profile a read query with ``EXPLAIN (FORMAT JSON)`` (never runs it)."""
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("EXPLAIN (FORMAT JSON) " + sql)
                row = cur.fetchone()
        except Exception as exc:
            raise ConnectorUnavailable(f"Query profiling failed: {exc}") from exc
        finally:
            self._safe_close(conn)

        plan = self._extract_root_plan(row)
        rows_produced: Optional[int] = None
        if plan is not None:
            raw_rows = plan.get("Plan Rows")
            if raw_rows is not None:
                try:
                    rows_produced = int(raw_rows)
                except (TypeError, ValueError):
                    rows_produced = None

        bytes_scanned = (
            rows_produced * _BYTES_PER_ROW if rows_produced is not None else None
        )

        return QueryStat(
            query_id=uuid.uuid4().hex,
            rows_produced=rows_produced,
            bytes_scanned=bytes_scanned,
        )

    # --------------------------------------------------------------- helpers
    @staticmethod
    def _extract_root_plan(row) -> Optional[dict]:
        """Pull the root ``Plan`` dict out of an EXPLAIN (FORMAT JSON) result.

        The driver returns either a JSON string or already-parsed list/dict in a
        single-cell row. The payload is ``[{"Plan": {...}}]``.
        """
        if not row:
            return None
        payload = row[0]
        if isinstance(payload, (str, bytes, bytearray)):
            try:
                payload = json.loads(payload)
            except (ValueError, TypeError):
                return None
        if isinstance(payload, list):
            payload = payload[0] if payload else None
        if isinstance(payload, dict):
            plan = payload.get("Plan")
            if isinstance(plan, dict):
                return plan
        return None

    @staticmethod
    def _safe_close(conn) -> None:
        try:
            conn.close()
        except Exception:  # pragma: no cover - defensive
            pass

    def close(self) -> None:  # nothing persistent is held open
        return None
