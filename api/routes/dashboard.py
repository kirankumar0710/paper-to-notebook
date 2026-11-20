"""``GET /``: the dashboard page, assembled from one HTML file per panel.

``static/index.html`` holds the layout and a marker for each panel, e.g.
``<!-- include: panels/config/config.html -->``; each marker is replaced
with that file. The panels' CSS and JavaScript are served from ``/static``.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

from api.settings import STATIC_DIR, UPLOAD_PAGE

__all__ = ["render_page", "router"]

INCLUDE = re.compile(r"<!--\s*include:\s*(\S+?)\s*-->")

router = APIRouter()


def _read_include(name: str, static_dir: Path) -> str:
    """Read one included file, which must be inside ``static_dir``."""
    path = (static_dir / name).resolve()
    if not path.is_relative_to(static_dir.resolve()) or not path.is_file():
        raise FileNotFoundError(f"Page include not found: {name}")
    return path.read_text(encoding="utf-8")


def render_page(
    page: Path = UPLOAD_PAGE, static_dir: Path = STATIC_DIR
) -> str:
    """Return ``page`` with every include marker replaced by its file."""
    return INCLUDE.sub(
        lambda match: _read_include(match.group(1), static_dir),
        page.read_text(encoding="utf-8"),
    )


@router.get("/", include_in_schema=False)
async def dashboard() -> HTMLResponse:
    """Serve the dashboard with all five panels."""
    try:
        html = render_page()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return HTMLResponse(html)
