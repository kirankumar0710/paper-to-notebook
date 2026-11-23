"""In-memory log of MCP activity, polled by the dashboard.

Every step of an arXiv lookup is recorded here: starting the MCP server,
the handshake, each tool call with its arguments, each result, and the PDF
download that follows. ``GET /api/mcp/events?after=<id>`` returns the new
events since the last one a client has seen.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any

__all__ = ["EVENT_KINDS", "MCP_LOG", "McpLog"]

# server: the MCP server process and handshake; call: a tool call and its
# arguments; result: what a tool returned; http: the app's own PDF download
# (not an MCP call, shown so the whole lookup is visible); error: anything
# that failed.
EVENT_KINDS = ("server", "call", "result", "http", "error")
MAX_EVENTS = 500
MAX_TEXT_CHARS = 400


def _clip(text: str | None) -> str | None:
    if text is None or len(text) <= MAX_TEXT_CHARS:
        return text
    return text[: MAX_TEXT_CHARS - 1] + "…"


class McpLog:
    """A bounded, thread-safe list of MCP events with increasing ids."""

    def __init__(self, max_events: int = MAX_EVENTS) -> None:
        self._events: deque[dict[str, Any]] = deque(maxlen=max_events)
        self._next_id = 1
        self._lock = threading.Lock()

    def add(
        self,
        kind: str,
        message: str,
        *,
        run: str | None = None,
        tool: str | None = None,
        arguments: dict[str, Any] | None = None,
        duration_ms: float | None = None,
        detail: str | None = None,
    ) -> dict[str, Any]:
        """Record one event and return it.

        Args:
            kind: One of :data:`EVENT_KINDS`.
            message: One line describing what happened.
            run: Groups the events of one lookup, e.g. ``"arXiv 1706.03762"``.
            tool: The MCP tool name, for ``call`` and ``result`` events.
            arguments: The tool call's arguments.
            duration_ms: How long the step took.
            detail: A longer note, e.g. a short summary of a tool's reply.
        """
        if kind not in EVENT_KINDS:
            raise ValueError(f"Unknown MCP event kind: {kind!r}")
        with self._lock:
            event = {
                "id": self._next_id,
                "time": time.time(),
                "kind": kind,
                "message": _clip(message),
                "run": run,
                "tool": tool,
                "arguments": arguments,
                "duration_ms": None
                if duration_ms is None
                else round(duration_ms, 1),
                "detail": _clip(detail),
            }
            self._next_id += 1
            self._events.append(event)
            return event

    def since(self, after: int = 0) -> dict[str, Any]:
        """Events with an id greater than ``after``, oldest first."""
        with self._lock:
            events = [e for e in self._events if e["id"] > after]
            return {"events": events, "last_id": self._next_id - 1}

    def clear(self) -> None:
        """Forget all events (ids keep increasing)."""
        with self._lock:
            self._events.clear()


# The log the server records to and the dashboard reads.
MCP_LOG = McpLog()
