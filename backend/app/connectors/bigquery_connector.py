"""Read-only BigQuery connector (Phase-2 live enrichment).

Wraps a live BigQuery project as a strictly read-only lens: it lists datasets +
tables, introspects table schemas, reads recent billing from
``INFORMATION_SCHEMA.JOBS_BY_PROJECT``, and - the key feature - profiles queries
with a **dry run**, which returns the EXACT number of bytes BigQuery would bill
without executing the query at all.

The driver (``google-cloud-bigquery``) is *optional*: it is imported lazily
inside :meth:`_client`, so this module imports cleanly with no driver installed
and raises :class:`ConnectorUnavailable` (never ``ImportError``) only when an
actual connection is attempted. Credentials are picked up from the ambient
Application Default Credentials (ADC).
"""
from __future__ import annotations

import uuid
from typing import List, Optional

from app.connectors.base import (
    ColumnInfo,
    Connector,
    ConnectorUnavailable,
    QueryStat,
    TableSchema,
)

# BigQuery on-demand analysis pricing: USD per TiB (2**40 bytes) scanned.
_PRICE_PER_TIB_USD = 6.25
_TIB = 2 ** 40


class BigQueryConnector(Connector):
    """Read-only BigQuery connector.

    Lazily builds a client (the constructor only stores config). All access is
    metadata introspection, dry-run profiling, and JOBS history views; the
    underlying query is never executed.
    """

    kind = "bigquery"
    warehouse = "bigquery"
    requires_credentials = True

    def __init__(self, config) -> None:
        # Do NOT connect here - the client is built lazily on first use.
        self._config = config

    # ------------------------------------------------------------ driver/conn
    def _driver(self):
        """Import the BigQuery library lazily.

        Raises :class:`ConnectorUnavailable` (never ``ImportError``) when the
        driver is missing.
        """
        try:
            from google.cloud import bigquery  # type: ignore
        except ImportError as exc:
            raise ConnectorUnavailable(
                "Install google-cloud-bigquery to use the bigquery connector."
            ) from exc
        return bigquery

    def _client(self):
        """Build a BigQuery client from ``config.project`` using ambient ADC."""
        bigquery = self._driver()
        project = getattr(self._config, "project", None)
        try:
            return bigquery.Client(project=project) if project else bigquery.Client()
        except Exception as exc:  # missing ADC creds / bad project
            raise ConnectorUnavailable(
                f"BigQuery client init failed (check ADC credentials): {exc}"
            ) from exc

    # ------------------------------------------------------------ connection
    def test_connection(self) -> bool:
        client = self._client()
        try:
            rows = list(client.query("SELECT 1").result())
            return bool(rows and rows[0][0] == 1)
        except Exception as exc:
            raise ConnectorUnavailable(
                f"BigQuery connection test failed: {exc}"
            ) from exc

    # --------------------------------------------------------------- tables
    def list_tables(self, schema: Optional[str] = None) -> List[str]:
        """List ``dataset.table`` names. ``schema`` selects a single dataset."""
        client = self._client()
        try:
            if schema:
                dataset_ids = [schema]
            else:
                dataset_ids = [ds.dataset_id for ds in client.list_datasets()]

            out: List[str] = []
            for dataset_id in dataset_ids:
                for tbl in client.list_tables(dataset_id):
                    out.append(f"{tbl.dataset_id}.{tbl.table_id}")
        except Exception as exc:
            raise ConnectorUnavailable(f"Failed to list tables: {exc}") from exc
        out.sort()
        return out

    # --------------------------------------------------------------- schema
    def get_schema(self, table: str) -> TableSchema:
        client = self._client()
        try:
            tbl = client.get_table(table)
        except Exception as exc:
            raise ConnectorUnavailable(
                f"Failed to read schema for {table!r}: {exc}"
            ) from exc

        columns: List[ColumnInfo] = []
        partition_columns: List[str] = []
        for field in tbl.schema:
            mode = (getattr(field, "mode", None) or "NULLABLE").upper()
            nullable = mode != "REQUIRED"
            columns.append(
                ColumnInfo(
                    name=field.name,
                    data_type=str(field.field_type),
                    nullable=nullable,
                )
            )

        # Surface the partitioning column, if any (helps cost reasoning).
        partitioning = getattr(tbl, "time_partitioning", None)
        if partitioning is not None and getattr(partitioning, "field", None):
            partition_columns.append(partitioning.field)
            for col in columns:
                if col.name == partitioning.field:
                    col.is_partition_key = True

        return TableSchema(
            name=tbl.table_id,
            schema_name=tbl.dataset_id,
            database=tbl.project,
            columns=columns,
            estimated_row_count=tbl.num_rows,
            partition_columns=partition_columns,
        )

    # -------------------------------------------------------------- history
    def query_history(
        self, *, table: Optional[str] = None, limit: int = 200
    ) -> List[QueryStat]:
        """Read recent jobs from ``INFORMATION_SCHEMA.JOBS_BY_PROJECT``.

        Returns ``[]`` when the driver is unavailable or the region view is not
        accessible - history is best-effort enrichment, never fatal.
        """
        try:
            client = self._client()
        except ConnectorUnavailable:
            return []
        sql = (
            "SELECT job_id, total_bytes_billed, total_slot_ms "
            "FROM `region-us`.INFORMATION_SCHEMA.JOBS_BY_PROJECT "
            "ORDER BY creation_time DESC "
            f"LIMIT {int(limit)}"
        )
        try:
            rows = list(client.query(sql).result())
        except Exception:
            return []

        out: List[QueryStat] = []
        for row in rows:
            job_id = row["job_id"]
            bytes_billed = row["total_bytes_billed"]
            slot_ms = row["total_slot_ms"]
            cost_usd = (
                (int(bytes_billed) / _TIB) * _PRICE_PER_TIB_USD
                if bytes_billed is not None
                else None
            )
            out.append(
                QueryStat(
                    query_id=str(job_id) if job_id is not None else uuid.uuid4().hex,
                    bytes_scanned=int(bytes_billed) if bytes_billed is not None else None,
                    cost_usd=cost_usd,
                    elapsed_ms=int(slot_ms) if slot_ms is not None else None,
                )
            )
        return out

    # -------------------------------------------------------------- profile
    def profile_query(self, sql: str) -> QueryStat:
        """Profile via a BigQuery **dry run** - exact billed bytes, query NOT run.

        ``dry_run=True`` asks BigQuery to validate and price the query without
        executing it; ``use_query_cache=False`` ensures the estimate reflects a
        real scan rather than a cached (zero-byte) result.
        """
        bigquery = self._driver()
        client = self._client()
        job_config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
        try:
            job = client.query(sql, job_config=job_config)
            bytes_scanned = job.total_bytes_processed
        except Exception as exc:
            raise ConnectorUnavailable(f"Query profiling failed: {exc}") from exc

        cost_usd = (
            (int(bytes_scanned) / _TIB) * _PRICE_PER_TIB_USD
            if bytes_scanned is not None
            else None
        )
        return QueryStat(
            query_id=uuid.uuid4().hex,
            bytes_scanned=int(bytes_scanned) if bytes_scanned is not None else None,
            cost_usd=cost_usd,
        )

    def close(self) -> None:  # the client holds no long-lived connection
        return None
