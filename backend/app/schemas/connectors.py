"""API schemas for the Phase-2 live database connectors.

The connector core (``app/connectors/base.py``) uses dataclasses for internal
work; these Pydantic models are the request/response shapes for the HTTP layer
(and mirror ``frontend/lib/types.ts``). Helpers convert the dataclasses across.
"""
from __future__ import annotations

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from app.connectors.base import ColumnInfo, QueryStat, TableSchema

ConnectorKind = Literal["duckdb", "postgres", "snowflake", "bigquery"]


class ConnectorConfig(BaseModel):
    """How to reach a database. Only the fields a given kind needs are used.

    The DuckDB "demo" connector needs no credentials and is always safe - it
    runs an in-process, read-only database over bundled sample data.
    """

    kind: ConnectorKind = "duckdb"
    dsn: Optional[str] = None  # postgres: postgresql://user:pass@host/db
    account: Optional[str] = None  # snowflake
    user: Optional[str] = None
    password: Optional[str] = None
    warehouse: Optional[str] = None
    database: Optional[str] = None
    schema_name: Optional[str] = None
    project: Optional[str] = None  # bigquery
    options: Dict[str, str] = Field(default_factory=dict)


class ColumnModel(BaseModel):
    name: str
    data_type: str
    nullable: bool = True
    is_partition_key: bool = False

    @classmethod
    def of(cls, c: ColumnInfo) -> "ColumnModel":
        return cls(
            name=c.name,
            data_type=c.data_type,
            nullable=c.nullable,
            is_partition_key=c.is_partition_key,
        )


class TableSchemaModel(BaseModel):
    name: str
    schema_name: Optional[str] = None
    database: Optional[str] = None
    columns: List[ColumnModel] = Field(default_factory=list)
    estimated_row_count: Optional[int] = None
    partition_columns: List[str] = Field(default_factory=list)

    @classmethod
    def of(cls, t: TableSchema) -> "TableSchemaModel":
        return cls(
            name=t.name,
            schema_name=t.schema_name,
            database=t.database,
            columns=[ColumnModel.of(c) for c in t.columns],
            estimated_row_count=t.estimated_row_count,
            partition_columns=list(t.partition_columns),
        )


class QueryStatModel(BaseModel):
    query_id: str = ""
    bytes_scanned: Optional[int] = None
    credits_used: Optional[float] = None
    cost_usd: Optional[float] = None
    elapsed_ms: Optional[int] = None
    rows_produced: Optional[int] = None

    @classmethod
    def of(cls, q: QueryStat) -> "QueryStatModel":
        return cls(
            query_id=q.query_id,
            bytes_scanned=q.bytes_scanned,
            credits_used=q.credits_used,
            cost_usd=q.cost_usd,
            elapsed_ms=q.elapsed_ms,
            rows_produced=q.rows_produced,
        )


class ConnectorInfo(BaseModel):
    kind: str
    label: str
    available: bool  # driver/deps importable in this deployment
    requires_credentials: bool
    enabled: bool  # allowed to connect in this deployment (gating)
    detail: str = ""


class ConnectorTestRequest(BaseModel):
    config: ConnectorConfig


class ConnectorTestResponse(BaseModel):
    ok: bool
    kind: str
    detail: str = ""
    tables: List[str] = Field(default_factory=list)


class IntrospectRequest(BaseModel):
    config: ConnectorConfig
    table: Optional[str] = None  # None -> introspect a sample of all tables


class IntrospectResponse(BaseModel):
    kind: str
    tables: List[TableSchemaModel] = Field(default_factory=list)
