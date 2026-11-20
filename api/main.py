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
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from api.dependencies import (
    check_configuration,
    get_llm_provider,
    get_output_dir,
)
from api.routes import config, convert, dashboard, download
from api.routes.config import describe_configuration, health
from api.routes.convert import (
    convert_pdf_to_notebook,
    convert_pdf_to_notebook_stream,
)
from api.settings import SAVED_PATH_HEADER, STATIC_DIR, UPLOAD_PAGE, VERSION
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


app = FastAPI(
    lifespan=lifespan,
    title="Paper to Notebook",
    description=(
        "Convert research paper PDFs into Google Colab notebooks with an LLM."
    ),
    version=VERSION,
)

# CORS — allow everything for demo purposes
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition", SAVED_PATH_HEADER],
)

# The dashboard and its panels' CSS/JS.
app.include_router(dashboard.router)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
# One router per panel: 1 Configuration, 2-4 Upload/Output/Metrics,
# 5 Download.
app.include_router(config.router)
app.include_router(convert.router)
app.include_router(download.router)
