"""MCP activity log panel: ``GET /api/mcp/events``.

The dashboard polls this endpoint for new MCP events (server start, tool
calls, results, the PDF download and errors) recorded by
:mod:`sources.arxiv`.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query

from sources.mcp_log import MCP_LOG

__all__ = ["router"]

router = APIRouter()


@router.get("/api/mcp/events")
async def mcp_events(
    after: Annotated[
        int, Query(ge=0, description="Return only events with a larger id.")
    ] = 0,
) -> dict[str, Any]:
    """MCP events newer than ``after``, oldest first.

    Returns ``events`` (each with ``id``, ``time`` in Unix seconds,
    ``kind`` of ``server``, ``call``, ``result``, ``http`` or ``error``,
    ``message``, ``run``, ``tool``, ``arguments``, ``duration_ms`` and
    ``detail``) and ``last_id``, to pass as ``after`` on the next poll.
    """
    return MCP_LOG.since(after)
