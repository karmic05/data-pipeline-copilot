"""Thread-safe in-memory store for analysis reports and their parse results.

A bounded :class:`collections.OrderedDict` keyed by ``AnalysisReport.id`` holds
at most :data:`MAX_ENTRIES` entries; the oldest entry is evicted FIFO when the
cap is exceeded. ``ParseResult`` objects are kept alongside reports so the
simulate/cost endpoints can re-run the cost and impact engines with new
parameters without re-parsing.
"""
from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from typing import Optional, Tuple

from app.schemas.ir import ParseResult
from app.schemas.report import AnalysisReport

logger = logging.getLogger(__name__)

#: Maximum number of analyses retained before FIFO eviction kicks in.
MAX_ENTRIES: int = 200

_lock = threading.Lock()
_entries: "OrderedDict[str, Tuple[AnalysisReport, Optional[ParseResult]]]" = (
    OrderedDict()
)


def save(report: AnalysisReport, parse_result: Optional[ParseResult] = None) -> None:
    """Store ``report`` (and optionally its ``parse_result``) under ``report.id``.

    Re-saving an existing id refreshes its position; once more than
    :data:`MAX_ENTRIES` analyses are held, the oldest entries are evicted FIFO.
    """
    with _lock:
        if report.id in _entries:
            _entries.pop(report.id)
        _entries[report.id] = (report, parse_result)
        while len(_entries) > MAX_ENTRIES:
            evicted_id, _ = _entries.popitem(last=False)
            logger.info("Store full (%d entries): evicted analysis %s", MAX_ENTRIES, evicted_id)


def get(report_id: str) -> Optional[AnalysisReport]:
    """Return the stored :class:`AnalysisReport` for ``report_id``, or ``None``."""
    with _lock:
        entry = _entries.get(report_id)
    return entry[0] if entry is not None else None


def get_parse_result(report_id: str) -> Optional[ParseResult]:
    """Return the stored :class:`ParseResult` for ``report_id``, or ``None``.

    ``None`` is returned both for unknown ids and for analyses saved without a
    parse result.
    """
    with _lock:
        entry = _entries.get(report_id)
    return entry[1] if entry is not None else None
