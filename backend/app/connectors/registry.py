"""Connector registry / factory.

Resolves a :class:`~app.connectors.base.Connector` from a kind + config, and
reports which connectors are available (driver importable) and enabled
(allowed to connect in this deployment). External credential-taking connectors
are gated behind ``settings.allow_live_connections`` for SSRF safety; the
in-process DuckDB demo connector is always allowed.
"""
from __future__ import annotations

import importlib
import logging
from typing import Dict, List, Tuple

from app.connectors.base import Connector, ConnectorUnavailable

logger = logging.getLogger(__name__)

# kind -> (module, class name, label, requires_credentials)
_REGISTRY: Dict[str, Tuple[str, str, str, bool]] = {
    "duckdb": ("app.connectors.duckdb_connector", "DuckDBConnector", "DuckDB (demo / local)", False),
    "postgres": ("app.connectors.postgres_connector", "PostgresConnector", "PostgreSQL", True),
    "snowflake": ("app.connectors.snowflake_connector", "SnowflakeConnector", "Snowflake", True),
    "bigquery": ("app.connectors.bigquery_connector", "BigQueryConnector", "BigQuery", True),
}


def _load_class(kind: str):
    if kind not in _REGISTRY:
        raise ConnectorUnavailable(f"Unknown connector kind: {kind!r}")
    module_name, class_name, _, _ = _REGISTRY[kind]
    module = importlib.import_module(module_name)
    return getattr(module, class_name)


def is_available(kind: str) -> bool:
    """Whether the connector's driver/module can be imported here."""
    try:
        _load_class(kind)
        return True
    except Exception:  # missing optional driver, etc.
        return False


def is_enabled(kind: str) -> bool:
    """Whether this deployment is allowed to open this connection.

    DuckDB demo is always enabled (safe, in-process). External connectors need
    ``settings.allow_live_connections`` so the public deploy cannot be used to
    reach arbitrary hosts.
    """
    if kind == "duckdb":
        return True
    try:
        from app.config import settings

        return bool(getattr(settings, "allow_live_connections", False))
    except Exception:
        return False


def get_connector(kind: str, config) -> Connector:
    """Instantiate a connector for ``kind`` with the given config object.

    ``config`` is an ``app.schemas.connectors.ConnectorConfig`` (or any object
    the connector understands). Raises :class:`ConnectorUnavailable` when the
    kind is unknown, its driver is missing, or it is disabled by gating.
    """
    if not is_enabled(kind):
        raise ConnectorUnavailable(
            f"The {kind!r} connector is disabled on this deployment. "
            "Set ALLOW_LIVE_CONNECTIONS=true on a trusted instance to enable "
            "external database connections."
        )
    cls = _load_class(kind)
    return cls(config)


def list_connectors() -> List[dict]:
    """Describe every registered connector (availability + gating)."""
    out: List[dict] = []
    for kind, (_, _, label, requires_creds) in _REGISTRY.items():
        available = is_available(kind)
        enabled = is_enabled(kind)
        if not available:
            detail = "Driver not installed in this deployment."
        elif not enabled:
            detail = "Disabled here (set ALLOW_LIVE_CONNECTIONS=true to enable)."
        elif requires_creds:
            detail = "Ready — provide connection credentials."
        else:
            detail = "Ready — no credentials needed (in-process demo)."
        out.append(
            {
                "kind": kind,
                "label": label,
                "available": available,
                "requires_credentials": requires_creds,
                "enabled": enabled,
                "detail": detail,
            }
        )
    return out
