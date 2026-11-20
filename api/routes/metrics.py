"""Panel 4 (Metrics): totals since the server started.

Per-conversion metrics travel in the ``result`` event of
``/convert/stream`` (see :mod:`api.routes.convert`); this module keeps the
running totals and serves them at ``GET /api/metrics/session``.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from fastapi import APIRouter

__all__ = ["record_failure", "record_success", "router", "session_totals"]

router = APIRouter()

_lock = threading.Lock()
_started = time.time()
_totals: dict[str, Any] = {
    "conversions": 0,
    "failed": 0,
    "input_tokens": 0,
    "output_tokens": 0,
    "total_tokens": 0,
    "cost_usd": 0.0,
    # Conversions whose cost could not be estimated (no usage or no price).
    "unpriced": 0,
}


def session_totals() -> dict[str, Any]:
    """A snapshot of the totals, with when the server started."""
    with _lock:
        return {
            **_totals,
            "cost_usd": round(_totals["cost_usd"], 6),
            "since": _started,
        }


def record_success(
    usage: dict[str, int] | None, cost: dict[str, Any] | None
) -> dict[str, Any]:
    """Add a finished conversion and return the new totals."""
    with _lock:
        _totals["conversions"] += 1
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            _totals[key] += (usage or {}).get(key, 0)
        if cost is None:
            _totals["unpriced"] += 1
        else:
            _totals["cost_usd"] += cost["total_usd"]
    return session_totals()


def record_failure() -> dict[str, Any]:
    """Count a failed conversion and return the new totals."""
    with _lock:
        _totals["failed"] += 1
    return session_totals()


@router.get("/api/metrics/session")
async def session() -> dict[str, Any]:
    """Conversions, tokens and estimated cost since the server started."""
    return session_totals()
