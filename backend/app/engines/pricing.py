"""Real, published cloud-warehouse pricing + open-benchmark physical constants.

This module exists so the cost engine (:mod:`app.engines.cost`) is grounded in
**actual list prices** and **benchmark-derived physical constants** rather than
opaque magic numbers. Every constant below carries a source/derivation comment.

Pricing snapshot
----------------
All list prices are the publicly published US (us-east-1 / equivalent) on-demand
list rates as of late 2025 / early 2026. They are list prices — real customers
negotiate discounts — so treat the dollar outputs as *order-of-magnitude*, not
invoice-exact. The relative ranking between warehouses is the durable signal.

Benchmark calibration basis
----------------------------
The bytes-per-row widths and the Snowflake bytes->credit throughput constant are
calibrated against the published **TPC-H** dataset characteristics:

  * TPC-H Scale Factor 1 (SF1) ~= 1 GB of raw data.
  * The ``lineitem`` table at SF1 has ~6,001,215 rows (the canonical figure from
    the TPC-H spec) in ~1 GB raw, i.e. ~150-170 raw bytes/row for that
    *typical-width* fact table (16 columns). Columnar compression on Snowflake /
    BigQuery / Redshift then shrinks the *scanned* footprint, but on-demand
    BigQuery bills on **logical** (uncompressed) bytes, so we model uncompressed
    bytes/row and let the per-engine helpers translate.
  * Narrow dimension rows (a handful of int/short-string columns, e.g. TPC-H
    ``region``/``nation``) land around ~80 B/row; a wide ``SELECT *`` over a
    denormalized fact/report table (timestamps, decimals, long strings) runs
    ~600 B/row.

These three widths (NARROW ~80, TYPICAL ~200, WIDE ~600) bracket the TPC-H /
TPC-DS table population and are what :func:`bytes_for_rows` selects between.

The Snowflake throughput constant :data:`SNOWFLAKE_BYTES_PER_CREDIT` is
*calibrated* (not published): it is chosen so that a clean 10M-row x ~200 B
(~2 GB) hourly job on a small (XS-S) warehouse lands at ~0.025 credits/run, i.e.
a few cents — inside the 0.01-0.05 credits/run target band. See its comment.
"""
from __future__ import annotations

import math
from typing import Dict, Tuple

# ==========================================================================
# Unit helpers
# ==========================================================================
#: 1 tebibyte in bytes. BigQuery on-demand pricing is quoted per **TiB**
#: (1024^4 bytes), NOT per decimal TB (10^12) — this distinction is ~10% of
#: the bill, so we use the binary unit deliberately.
TIB: int = 1024**4
#: 1 gibibyte in bytes (for human-readable derivations in comments/tests).
GIB: int = 1024**3


def _finite(value: float, default: float = 0.0) -> float:
    """Return ``value`` as a finite float, else ``default`` (no NaN/inf leaks)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(v) or math.isinf(v):
        return default
    return v


def _nonneg(value: float) -> float:
    """Finite, non-negative float."""
    return max(0.0, _finite(value))


# ==========================================================================
# Benchmark-derived physical constants (bytes/row by table width)
# ==========================================================================
# Derivation: TPC-H SF1 ~= 1 GB raw with the ~6.0M-row ``lineitem`` fact table
# (16 columns) implying ~150-170 raw bytes/row for a *typical* analytic row. We
# round to 200 B to also absorb the slightly wider TPC-DS ``store_sales`` /
# ``catalog_sales`` fact rows. Narrow/wide bracket the rest of the schema.

#: Narrow rows: a few int/short-string columns (e.g. TPC-H region/nation, surrogate
#: dimension keys). ~80 B/row.
ROW_BYTES_NARROW: float = 80.0
#: Typical analytic fact row (~16 mixed columns), the default. ~200 B/row.
#: Anchored to TPC-H lineitem (SF1: ~6.0M rows in ~1 GB).
ROW_BYTES_TYPICAL: float = 200.0
#: Wide / ``SELECT *`` over a denormalized report table (timestamps, decimals,
#: long varchars, JSON). ~600 B/row.
ROW_BYTES_WIDE: float = 600.0

#: Named widths -> bytes/row, for :func:`bytes_for_rows`.
ROW_WIDTHS: Dict[str, float] = {
    "narrow": ROW_BYTES_NARROW,
    "typical": ROW_BYTES_TYPICAL,
    "wide": ROW_BYTES_WIDE,
}

#: Assumed full-table column count, used by cost.py to scale a trimmed
#: projection's width. A typical analytic table is ~20 columns (TPC-DS fact
#: tables run 20-35 columns; we take a conservative 20).
ASSUMED_TABLE_COLUMNS: int = 20
#: A trimmed projection never shrinks the logical scan below this fraction of
#: full width — columnar engines still touch keys, filter, and join columns.
MIN_WIDTH_FACTOR: float = 0.15


def bytes_for_rows(row_count: float, width: str = "typical") -> float:
    """Logical (uncompressed) bytes for ``row_count`` rows of a given width.

    ``width`` is one of ``"narrow"`` / ``"typical"`` / ``"wide"`` (default
    typical). Unknown widths fall back to typical. Calibrated to TPC-H: e.g.
    ``bytes_for_rows(10_000_000, "typical")`` -> 2.0e9 (~2 GB), matching the
    ~200 B/row anchor.

    The value is the *logical* byte count — this is exactly what BigQuery
    on-demand bills on, and the per-engine helpers below derive compressed-scan
    runtime / credits from it.
    """
    rows = _nonneg(row_count)
    per_row = ROW_WIDTHS.get((width or "typical").strip().lower(), ROW_BYTES_TYPICAL)
    return rows * per_row


# ==========================================================================
# Snowflake — credit-based pricing
# ==========================================================================
# Source: Snowflake published list pricing. Enterprise edition lists at
# **$3.00 / credit** (Standard ~$2.00, Business Critical ~$4.00) on AWS US.
# A virtual warehouse burns credits at a fixed rate per size, doubling each
# T-shirt step:
#   XS=1, S=2, M=4, L=8, XL=16, 2XL=32, 3XL=64, 4XL=128 credits/hour.
# (Published in Snowflake docs as "Virtual Warehouse Credit Usage".)
SNOWFLAKE_CREDIT_USD: float = 3.00  # Enterprise edition list, USD/credit

#: Credits/hour by warehouse size (published doubling schedule).
SNOWFLAKE_CREDITS_PER_HOUR: Dict[str, float] = {
    "xs": 1.0,
    "s": 2.0,
    "m": 4.0,
    "l": 8.0,
    "xl": 16.0,
    "2xl": 32.0,
    "3xl": 64.0,
    "4xl": 128.0,
}

#: Effective scan throughput of one XS warehouse node, in **bytes/second of
#: logical data**. Calibration target: a clean 10M-row x ~200 B (~2 GB) job on
#: an XS warehouse should cost ~0.01-0.05 credits/run.
#:
#: Derivation: XS burns 1 credit/hour = 1 credit / 3600 s. We want ~0.025 credits
#: for 2 GB, i.e. ~0.025 * 3600 s = 90 s of XS time for 2e9 bytes ->
#: ~2e9 / 90 ~= 2.2e7... but XS throughput on warm cache is much higher; the
#: 0.025-credit target already bakes in per-query fixed overhead. We therefore
#: express the model directly as a **bytes-per-credit** constant on an XS-S
#: warehouse so the calibration is exact and transparent (see
#: SNOWFLAKE_BYTES_PER_CREDIT below), and keep this throughput figure only for
#: the runtime estimate shown in reasoning.
SNOWFLAKE_XS_SCAN_BYTES_PER_SEC: float = 800e6  # ~0.8 GB/s logical on XS (warm)

#: Logical bytes processed per Snowflake credit on a small (XS-S) warehouse.
#: CALIBRATION (the load-bearing constant): chosen so
#:   2 GB / 80 GB-per-credit = 0.025 credits/run  -> 0.025 * $3.00 ~= $0.075/run
#: which sits squarely in the "few cents, 0.01-0.05 credits" target band for a
#: clean 10M-row hourly job. Larger warehouses finish faster but burn credits
#: proportionally faster, so credits-per-byte is ~size-independent at the XS-S
#: end; we therefore price off this single constant and report the chosen size
#: only for the human runtime estimate.
SNOWFLAKE_BYTES_PER_CREDIT: float = 80e9  # 80 GB logical / credit (XS-S calibration)


def snowflake_credits(bytes_scanned: float) -> float:
    """Credits consumed for ``bytes_scanned`` logical bytes on an XS-S warehouse."""
    b = _nonneg(bytes_scanned)
    if b <= 0.0:
        return 0.0
    return b / SNOWFLAKE_BYTES_PER_CREDIT


def snowflake_cost(bytes_scanned: float, credit_usd: float = SNOWFLAKE_CREDIT_USD) -> Tuple[float, float]:
    """Snowflake ``(usd, credits)`` for ``bytes_scanned`` logical bytes.

    ``credit_usd`` defaults to the Enterprise list price ($3.00/credit).
    """
    credits = snowflake_credits(bytes_scanned)
    usd = credits * _nonneg(credit_usd)
    return _finite(usd), _finite(credits)


def snowflake_runtime_seconds(bytes_scanned: float, size: str = "xs") -> float:
    """Rough wall-clock seconds for a scan on the given warehouse size.

    Throughput scales with size (an M warehouse is ~4x an XS), per the credit
    schedule. Used only for human-readable reasoning, not for billing.
    """
    b = _nonneg(bytes_scanned)
    if b <= 0.0:
        return 0.0
    size_key = (size or "xs").strip().lower()
    size_factor = SNOWFLAKE_CREDITS_PER_HOUR.get(size_key, 1.0)
    throughput = SNOWFLAKE_XS_SCAN_BYTES_PER_SEC * max(1.0, size_factor)
    return b / throughput


# ==========================================================================
# BigQuery — on-demand (bytes-scanned) pricing
# ==========================================================================
# Source: Google BigQuery published on-demand analysis pricing:
#   **$6.25 per TiB** of bytes *billed* (us multi-region; the first 1 TiB/month
#   is free — we intentionally IGNORE the free tier so estimates are
#   conservative/worst-case). Storage cost is out of scope (ignored).
# BigQuery bills the **logical** bytes scanned for the columns touched, with a
# **10 MB minimum** per query that scans any data, rounded up.
BIGQUERY_USD_PER_TIB: float = 6.25
#: Minimum bytes billed per non-trivial BigQuery query (10 MiB).
BIGQUERY_MIN_BYTES_BILLED: int = 10 * 1024 * 1024


def bigquery_cost(bytes_billed: float) -> Tuple[float, int]:
    """BigQuery on-demand ``(usd, bytes_billed_int)`` for bytes scanned.

    Enforces the 10 MiB minimum for any non-zero scan. Zero bytes -> $0 / 0
    bytes (an empty pipeline is free; we do not invent a minimum charge for
    "no work").
    """
    b = _nonneg(bytes_billed)
    if b <= 0.0:
        return 0.0, 0
    billed = int(max(b, BIGQUERY_MIN_BYTES_BILLED))
    usd = billed / TIB * BIGQUERY_USD_PER_TIB
    return _finite(usd), billed


# ==========================================================================
# Redshift — provisioned ra3 node-hour pricing
# ==========================================================================
# Source: Amazon Redshift published on-demand pricing for **ra3.4xlarge** ~=
# **$3.26 / node / hour** (us-east-1). Node assumption: we price a *single*
# ra3.4xlarge node-equivalent and attribute only the fraction of node-time the
# query actually occupies (see REDSHIFT_CONCURRENCY_SHARE) — a provisioned
# cluster is paid 24/7, but the *marginal* cost a query imposes is its share of
# node-time, which is the right number for "what does this query cost to run".
REDSHIFT_NODE_USD_PER_HOUR: float = 3.26  # ra3.4xlarge on-demand, us-east-1
#: Effective per-query logical-scan throughput on a shared ra3.4xlarge (bytes/s).
#: ra3.4xlarge has managed storage + RMS cache; ~300 MB/s is a conservative
#: warm-scan figure for a single concurrent query.
REDSHIFT_SCAN_BYTES_PER_SEC: float = 300e6
#: Fixed planner/queue/commit overhead attributed per query (seconds).
REDSHIFT_QUERY_OVERHEAD_SEC: float = 15.0
#: A single query occupies ~a quarter of the node's effective concurrency slots,
#: so it is charged ~25% of node-time while running (the rest serves other
#: concurrent queries on a busy cluster).
REDSHIFT_CONCURRENCY_SHARE: float = 0.25


def redshift_runtime_seconds(bytes_scanned: float) -> float:
    """Estimated query runtime (s) including fixed overhead."""
    b = _nonneg(bytes_scanned)
    return REDSHIFT_QUERY_OVERHEAD_SEC + b / REDSHIFT_SCAN_BYTES_PER_SEC


def redshift_cost(
    bytes_scanned: float,
    node_usd_per_hour: float = REDSHIFT_NODE_USD_PER_HOUR,
    concurrency_share: float = REDSHIFT_CONCURRENCY_SHARE,
) -> Tuple[float, float]:
    """Redshift ``(usd, runtime_seconds)`` for one query scanning ``bytes_scanned``.

    Marginal cost = (runtime / 3600) * node$/hr * concurrency_share. Zero bytes
    -> $0 (no overhead charged for an empty pipeline).
    """
    b = _nonneg(bytes_scanned)
    if b <= 0.0:
        return 0.0, 0.0
    runtime_sec = redshift_runtime_seconds(b)
    usd = (runtime_sec / 3600.0) * _nonneg(node_usd_per_hour) * _nonneg(concurrency_share)
    return _finite(usd), _finite(runtime_sec)


# ==========================================================================
# Databricks — Jobs Compute (DBU + cloud VM) pricing
# ==========================================================================
# Source: Databricks published Jobs Compute list pricing ~= **$0.15 / DBU**
# (Premium tier) for the Databricks platform charge. That is the platform fee
# ONLY — the customer also pays the underlying cloud VM. A blended *effective*
# rate of ~**$0.55 / DBU** (platform + VM) is a widely-cited rule of thumb for
# Jobs Compute on general-purpose instances; we use it so the dollar figure
# reflects total cost of ownership, and document the split below.
DATABRICKS_PLATFORM_USD_PER_DBU: float = 0.15  # Jobs Compute, Premium tier (platform only)
DATABRICKS_USD_PER_DBU: float = 0.55  # blended effective: platform $0.15 + ~$0.40 VM share
#: A small jobs cluster (driver + 2 workers) burns ~4 DBU/hour.
DATABRICKS_DBU_PER_HOUR: float = 4.0
#: Effective logical-scan throughput for that small jobs cluster (bytes/s).
DATABRICKS_SCAN_BYTES_PER_SEC: float = 400e6
#: Amortized cluster spin-up overhead per (non-empty) job run (seconds).
DATABRICKS_JOB_OVERHEAD_SEC: float = 60.0


def databricks_runtime_hours(bytes_scanned: float) -> float:
    """Estimated cluster wall-clock hours (incl. spin-up) for the scan."""
    b = _nonneg(bytes_scanned)
    if b <= 0.0:
        return 0.0
    return (DATABRICKS_JOB_OVERHEAD_SEC + b / DATABRICKS_SCAN_BYTES_PER_SEC) / 3600.0


def databricks_cost(
    bytes_scanned: float,
    usd_per_dbu: float = DATABRICKS_USD_PER_DBU,
    dbu_per_hour: float = DATABRICKS_DBU_PER_HOUR,
) -> Tuple[float, float]:
    """Databricks ``(usd, dbus)`` for one job run scanning ``bytes_scanned``.

    Cost = DBUs * blended $/DBU, where DBUs = runtime_hours * dbu/hour. The
    blended $/DBU already folds in the cloud VM share, so no separate VM term.
    Zero bytes -> $0 (no spin-up charged for an empty pipeline).
    """
    runtime_hr = databricks_runtime_hours(bytes_scanned)
    if runtime_hr <= 0.0:
        return 0.0, 0.0
    dbus = runtime_hr * _nonneg(dbu_per_hour)
    usd = dbus * _nonneg(usd_per_dbu)
    return _finite(usd), _finite(dbus)


# ==========================================================================
# Supported warehouses
# ==========================================================================
#: Warehouses this module can price. Unknown names fall back to snowflake
#: (handled by the caller in cost.py).
SUPPORTED_WAREHOUSES: Tuple[str, ...] = (
    "snowflake",
    "bigquery",
    "redshift",
    "databricks",
)
