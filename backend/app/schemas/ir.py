"""Core intermediate representation (IR).

Every supported input format (SQL, Airflow, dbt, Spark, Prefect/Dagster, Flink,
Kafka Streams, Great Expectations) is parsed into this single unified schema.
The rule engine, cost estimator, lineage engine, scorer, impact simulator and
LLM reasoning layer all consume the IR — never raw source code.
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

PipelineFormat = Literal[
    "sql",
    "airflow",
    "dbt",
    "spark",
    "prefect",
    "flink",
    "kafka",
    "great_expectations",
]
AccessType = Literal["read", "write", "readwrite"]
TransformationType = Literal["direct", "expression", "aggregation", "window", "unknown"]


class Location(BaseModel):
    line: int = 0
    col: int = 0


class TableRef(BaseModel):
    """A table / model / topic / dataset referenced by the pipeline."""

    name: str
    alias: Optional[str] = None
    schema_name: Optional[str] = None
    database: Optional[str] = None
    columns: List[str] = Field(default_factory=list)
    access_type: AccessType = "read"


class Operation(BaseModel):
    """A logical operation discovered in the pipeline.

    ``type`` is an open vocabulary. Canonical values used by the rule engine:

    - SQL:       SELECT, JOIN, AGGREGATE, WINDOW, SUBQUERY, CTE, INSERT, UPDATE,
                 DELETE, MERGE, FILTER, PARTITION_FILTER, ORDER_BY, DISTINCT,
                 UNION, LIMIT
    - Airflow:   TASK, SENSOR, XCOM_PUSH, XCOM_PULL, CALLBACK, DYNAMIC_DAG
    - Spark:     READ, WRITE, JOIN, GROUP_BY, WINDOW, REPARTITION, CACHE,
                 CHECKPOINT, WATERMARK, COLLECT, BROADCAST
    - Streaming: SOURCE, SINK, WINDOW, STATE, WATERMARK, CHECKPOINT, AGGREGATE

    ``details`` carries rule-relevant structured facts, e.g. for a JOIN:
    ``{"kind": "INNER", "has_on_clause": false, "left": "a", "right": "b",
    "condition": "a.id = b.id", "implicit_cast": false}``; for an Airflow TASK:
    ``{"task_id": "load", "operator": "PythonOperator", "retries": 0,
    "pool": null, "sla": null}``.
    """

    type: str
    location: Location = Field(default_factory=Location)
    details: Dict[str, Any] = Field(default_factory=dict)


class Dependency(BaseModel):
    """Edge in the task/table dependency graph (``source`` -> ``target``)."""

    source: str
    target: str
    type: Literal["reads_from", "writes_to", "triggers", "depends_on", "references"] = (
        "depends_on"
    )


class ColumnLineage(BaseModel):
    output_table: str
    output_column: str
    source_table: str
    source_column: str
    transformation: TransformationType = "unknown"
    expression: Optional[str] = None


class Scheduling(BaseModel):
    cron: Optional[str] = None
    interval_minutes: Optional[int] = None
    sla_minutes: Optional[int] = None
    catchup: Optional[bool] = None
    retries: Optional[int] = None


class Materialization(BaseModel):
    type: Literal["table", "view", "incremental", "ephemeral", "stream", "unknown"] = (
        "unknown"
    )
    strategy: Optional[str] = None
    partition_by: List[str] = Field(default_factory=list)
    cluster_by: List[str] = Field(default_factory=list)


class IRMetadata(BaseModel):
    estimated_row_count: Optional[int] = None
    warehouse_size: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    name: Optional[str] = None
    description: Optional[str] = None


class IR(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    format: PipelineFormat
    dialect: Optional[str] = None
    tables: List[TableRef] = Field(default_factory=list)
    operations: List[Operation] = Field(default_factory=list)
    dependencies: List[Dependency] = Field(default_factory=list)
    column_lineage: List[ColumnLineage] = Field(default_factory=list)
    scheduling: Scheduling = Field(default_factory=Scheduling)
    materialization: Materialization = Field(default_factory=Materialization)
    metadata: IRMetadata = Field(default_factory=IRMetadata)

    def ops(self, *types: str) -> List[Operation]:
        """All operations whose type is in ``types`` (convenience for rules)."""
        wanted = set(types)
        return [op for op in self.operations if op.type in wanted]

    def tables_by_access(self, access: AccessType) -> List[TableRef]:
        return [
            t
            for t in self.tables
            if t.access_type == access or t.access_type == "readwrite"
        ]


class ParseResult(BaseModel):
    """IR plus parser artifacts analyzers may need. Never serialized to the LLM.

    ``ast`` holds the raw parse tree (list of sqlglot Expressions for SQL/dbt/
    Flink, an ``ast.Module`` for Python formats, a dict for YAML formats).
    ``extras`` holds format-specific side data, e.g. for dbt:
    ``{"schema_yml": {...}, "refs": [...], "sources": [...]}``.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    ir: IR
    source: str
    ast: Any = Field(default=None, exclude=True)
    extras: Dict[str, Any] = Field(default_factory=dict)
    warnings: List[str] = Field(default_factory=list)

    @property
    def lines(self) -> List[str]:
        return self.source.splitlines()


class ParseError(Exception):
    """Raised when input cannot be parsed into an IR."""

    def __init__(self, message: str, line: Optional[int] = None) -> None:
        super().__init__(message)
        self.message = message
        self.line = line
