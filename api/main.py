"""FastAPI server: upload a research paper PDF, get a Colab notebook back.

The PDF is sent to the LLM selected by the ``PROVIDER`` environment
variable, which reads the paper and writes the notebook.

This module creates the app; the endpoints live in :mod:`api.routes`, one
module per dashboard panel, and the page in ``api/static``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from starlette.types import Scope

from api.dependencies import (
    check_configuration,
    get_llm_provider,
    get_output_dir,
)
from api.routes import config, convert, dashboard, download, mcp, metrics
from api.routes.config import describe_configuration, health
from api.routes.convert import (
    convert_pdf_to_notebook,
    convert_pdf_to_notebook_stream,
)
from api.settings import STATIC_DIR, UPLOAD_PAGE, VERSION
from util.log import configure_logging, get_logger

__all__ = [
    "UPLOAD_PAGE",
    "VERSION",
    "app",
    "check_configuration",
    "convert_pdf_to_notebook",
    "convert_pdf_to_notebook_stream",
    "describe_configuration",
    "get_llm_provider",
    "get_output_dir",
    "health",
]

configure_logging()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Warn at startup about missing configuration."""
    problems = check_configuration()
    for problem in problems:
        logger.warning("Configuration problem: %s", problem)
    if problems:
        logger.warning(
            "/convert will return 503 until this is fixed and the server "
            "is restarted."
        )
    yield


class RevalidatedStaticFiles(StaticFiles):
    """Static files the browser must re-check before reusing.

    Without a ``Cache-Control`` header, browsers keep CSS and JS for a
    while based on their age, so after an update the dashboard could load
    new HTML with old styles. ``no-cache`` makes them ask every time; an
    unchanged file is answered with a cheap 304 using its ETag.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


app = FastAPI(
    lifespan=lifespan,
    title="Paper to Notebook",
    description=(
        "Convert research paper PDFs into Google Colab notebooks with an LLM."
    ),
    version=VERSION,
)

# No CORS: the dashboard is served from this same origin, so browsers
# need no cross-origin permission, and other websites must not be able to
# read this API from a visitor's browser. Cross-site POSTs (which browsers
# send without asking) are refused by require_same_origin on /convert.

# The dashboard and its panels' CSS/JS.
app.include_router(dashboard.router)
app.mount(
    "/static", RevalidatedStaticFiles(directory=STATIC_DIR), name="static"
)
# One router per panel: 1 Configuration, 2-4 Upload/Output/Metrics
# (convert streams the per-run metrics; metrics keeps session totals),
# 5 Download.
app.include_router(config.router)
app.include_router(convert.router)
app.include_router(metrics.router)
app.include_router(download.router)
# MCP activity log: arXiv lookups through the arXiv MCP server.
app.include_router(mcp.router)
