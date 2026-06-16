"""Read-only Snowflake connector (Phase-2 live enrichment).

Wraps a live Snowflake account as a strictly read-only lens: it introspects
``INFORMATION_SCHEMA``, reads recent billing from
``SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY``, and profiles queries with
``EXPLAIN USING JSON`` — it never executes the user's query nor any DML/DDL.

The driver (``snowflake-connector-python``) is *optional*: it is imported lazily
inside :meth:`_connect`, so this module imports cleanly with no driver installed
and raises :class:`ConnectorUnavailable` (never ``ImportError``) only when an
actual connection is attempted.
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

# Snowflake Enterprise on-demand list price: USD per credit.
_CREDIT_PRICE_USD = 3.00


class SnowflakeConnector(Connector):
    """Read-only Snowflake connector.

    Lazily connects (the constructor only stores config). All access is
    introspection, ``EXPLAIN`` profiling, and ACCOUNT_USAGE history views; the
    underlying query is never run.
    """

    kind = "snowflake"
    warehouse = "snowflake"
    requires_credentials = True

    def __init__(self, config) -> None:
        # Do NOT connect here — connection is established lazily on first use.
        self._config = config

    # ------------------------------------------------------------ driver/conn
    def _connect(self):
        """Open a new live connection, importing the driver lazily.

        Raises :class:`ConnectorUnavailable` (never ``ImportError``) when the
        driver is missing or required credentials are absent.
        """
        try:
            import snowflake.connector as sf  # type: ignore
        except ImportError as exc:
            raise ConnectorUnavailable(
                "Install snowflake-connector-python to use the snowflake connector."
            ) from exc

        account = getattr(self._config, "account", None)
        user = getattr(self._config, "user", None)
        if not account or not user:
            raise ConnectorUnavailable(
                "Snowflake requires at least 'account' and 'user' credentials."
            )

        kwargs = {"account": account, "user": user}
        for attr, key in (
            ("password", "password"),
            ("warehouse", "warehouse"),
            ("database", "database"),
            ("schema_name", "schema"),
        ):
            value = getattr(self._config, attr, None)
            if value:
                kwargs[key] = value

        try:
            return sf.connect(**kwargs)
        except Exception as exc:  # network / auth / config error
            raise ConnectorUnavailable(
                f"Snowflake connection failed: {exc}"
            ) from exc

    # ------------------------------------------------------------ connection
    def test_connection(self) -> bool:
        conn = self._connect()
        try:
            cur = conn.cursor()
            try:
                cur.execute("SELECT 1")
                row = cur.fetchone()
            finally:
                cur.close()
            return bool(row and row[0] == 1)
        except Exception as exc:
            raise ConnectorUnavailable(
                f"Snowflake connection test failed: {exc}"
            ) from exc
        finally:
            self._safe_close(conn)

    # --------------------------------------------------------------- tables
    def list_tables(self, schema: Optional[str] = None) -> List[str]:
        sql = (
            "SELECT TABLE_SCHEMA, TABLE_NAME FROM INFORMATION_SCHEMA.TABLES"
        )
        params: list = []
        if schema:
            sql += " WHERE TABLE_SCHEMA = %s"
            params.append(schema)
        sql += " ORDER BY TABLE_SCHEMA, TABLE_NAME"

        conn = self._connect()
        try:
            cur = conn.cursor()
            try:
                cur.execute(sql, params or None)
                rows = cur.fetchall()
            finally:
                cur.close()
        except Exception as exc:
            raise ConnectorUnavailable(f"Failed to list tables: {exc}") from exc
        finally:
            self._safe_close(conn)
        return [f"{s}.{t}" for (s, t) in rows]

    # --------------------------------------------------------------- schema
    def get_schema(self, table: str) -> TableSchema:
        if "." in table:
            schema_name, table_name = table.rsplit(".", 1)
        else:
            schema_name, table_name = None, table

        conn = self._connect()
        try:
            cur = conn.cursor()
            try:
                col_sql = (
                    "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE "
                    "FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = %s"
                )
                params: list = [table_name]
                if schema_name:
                    col_sql += " AND TABLE_SCHEMA = %s"
                    params.append(schema_name)
                col_sql += " ORDER BY ORDINAL_POSITION"
                cur.execute(col_sql, params)
                rows = cur.fetchall()

                estimated_row_count: Optional[int] = None
                try:
                    rc_sql = (
                        "SELECT ROW_COUNT FROM INFORMATION_SCHEMA.TABLES "
                        "WHERE TABLE_NAME = %s"
                    )
                    rc_params: list = [table_name]
                    if schema_name:
                        rc_sql += " AND TABLE_SCHEMA = %s"
                        rc_params.append(schema_name)
                    cur.execute(rc_sql, rc_params)
                    rc_row = cur.fetchone()
                    if rc_row is not None and rc_row[0] is not None:
                        estimated_row_count = int(rc_row[0])
                except Exception:  # best-effort estimate
                    estimated_row_count = None
            finally:
                cur.close()
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
        """Read recent executions from ``SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY``.

        Returns ``[]`` when the driver is unavailable or the view is not
        accessible (insufficient grants) — history is best-effort, never fatal.
        """
        try:
            conn = self._connect()
        except ConnectorUnavailable:
            return []
        try:
            cur = conn.cursor()
            try:
                cur.execute(
                    """
                    SELECT QUERY_ID, BYTES_SCANNED,
                           CREDITS_USED_CLOUD_SERVICES,
                           TOTAL_ELAPSED_TIME, ROWS_PRODUCED
                    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
                    ORDER BY START_TIME DESC
                    LIMIT %s
                    """,
                    [int(limit)],
                )
                rows = cur.fetchall()
            finally:
                cur.close()
        except Exception:
            return []
        finally:
            self._safe_close(conn)

        out: List[QueryStat] = []
        for query_id, bytes_scanned, credits, elapsed, rows_produced in rows:
            credits_used = (
                float(credits) if credits is not None else None
            )
            cost_usd = (
                credits_used * _CREDIT_PRICE_USD
                if credits_used is not None
                else None
            )
            out.append(
                QueryStat(
                    query_id=str(query_id) if query_id is not None else uuid.uuid4().hex,
                    bytes_scanned=int(bytes_scanned) if bytes_scanned is not None else None,
                    credits_used=credits_used,
                    cost_usd=cost_usd,
                    elapsed_ms=int(elapsed) if elapsed is not None else None,
                    rows_produced=int(rows_produced) if rows_produced is not None else None,
                )
            )
        return out

    # -------------------------------------------------------------- profile
    def profile_query(self, sql: str) -> QueryStat:
        """Profile a read query with ``EXPLAIN USING JSON`` (never runs it)."""
        conn = self._connect()
        try:
            cur = conn.cursor()
            try:
                cur.execute("EXPLAIN USING JSON " + sql)
                row = cur.fetchone()
            finally:
                cur.close()
        except Exception as exc:
            raise ConnectorUnavailable(f"Query profiling failed: {exc}") from exc
        finally:
            self._safe_close(conn)

        plan = self._parse_explain_json(row)
        bytes_scanned = self._find_metric(plan, "bytesAssigned")
        rows_produced = self._find_metric(plan, "partitionsAssigned")

        return QueryStat(
            query_id=uuid.uuid4().hex,
            bytes_scanned=bytes_scanned,
            rows_produced=rows_produced,
        )

    # --------------------------------------------------------------- helpers
    @staticmethod
    def _parse_explain_json(row) -> Optional[dict]:
        """Parse the single-cell ``EXPLAIN USING JSON`` result into a dict."""
        if not row:
            return None
        payload = row[0]
        if isinstance(payload, (str, bytes, bytearray)):
            try:
                payload = json.loads(payload)
            except (ValueError, TypeError):
                return None
        return payload if isinstance(payload, dict) else None

    @classmethod
    def _find_metric(cls, payload, key: str) -> Optional[int]:
        """Recursively find the first integer ``key`` in the EXPLAIN plan tree.

        Snowflake's JSON plan nests operators; the global stats (e.g.
        ``partitionsAssigned`` / ``bytesAssigned``) may live at the root or in a
        ``GlobalStats`` block depending on version, so search the whole tree.
        """
        if payload is None:
            return None
        if isinstance(payload, dict):
            if key in payload:
                value = payload[key]
                try:
                    return int(value)
                except (TypeError, ValueError):
                    pass
            for sub in payload.values():
                found = cls._find_metric(sub, key)
                if found is not None:
                    return found
        elif isinstance(payload, list):
            for item in payload:
                found = cls._find_metric(item, key)
                if found is not None:
                    return found
        return None

    @staticmethod
    def _safe_close(conn) -> None:
        try:
            conn.close()
        except Exception:  # pragma: no cover - defensive
            pass

    def close(self) -> None:  # nothing persistent is held open
        return None
