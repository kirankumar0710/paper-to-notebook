"""FastAPI server: upload a research paper PDF, get a Colab notebook back.

The PDF is sent to the LLM selected by the ``PROVIDER`` environment
variable, which reads the paper and writes the notebook.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache, partial
from pathlib import Path
from typing import Annotated, Any

import nbformat
from fastapi import Depends, FastAPI, File, HTTPException, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse

from notebook_builder.converter import ConvertedNotebook, pdf_to_notebook
from providers import LLMProvider, ProviderError, get_provider
from providers.base import Progress
from providers.factory import resolve_settings
from util.config import ConfigError, resolve_output_dir
from util.files import save_unique
from util.log import configure_logging, get_logger

__all__ = ["app", "check_configuration", "get_llm_provider", "get_output_dir"]

VERSION = "0.2.0"
# The PDF is sent inline (base64, ~33% larger), so keep it well under the
# providers' request size limits.
MAX_UPLOAD_MB = 15
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
PDF_MAGIC = b"%PDF-"
IPYNB_MEDIA_TYPE = "application/x-ipynb+json"
# Suggested wait before retrying when the model is temporarily unavailable.
RETRY_AFTER_SECONDS = 60
UPLOAD_PAGE = Path(__file__).resolve().parent / "static" / "index.html"
SAVED_PATH_HEADER = "X-Notebook-Path"

configure_logging()
logger = get_logger(__name__)


def check_configuration() -> list[str]:
    """Return a message for every required setting that is missing.

    Called at startup so problems show up in the log immediately, rather
    than on the first upload.
    """
    problems = []
    for check in (resolve_settings, resolve_output_dir):
        try:
            check()
        except (ConfigError, FileNotFoundError) as exc:
            problems.append(str(exc))
    return problems


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


def _log_failure(detail: str, exc: Exception, provider: LLMProvider) -> None:
    """Log why a conversion failed, including the SDK's underlying error."""
    cause = exc.__cause__
    logger.error(
        "/convert failed (provider=%s, model=%s): %s%s",
        provider.name,
        provider.model,
        detail,
        f" | cause: {type(cause).__name__}: {cause}" if cause else "",
    )


@lru_cache(maxsize=1)
def get_llm_provider() -> LLMProvider:
    """Create the configured provider once and reuse it across requests."""
    try:
        return get_provider()
    except (ConfigError, ProviderError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"LLM provider is not configured: {exc}",
        ) from exc


def get_output_dir() -> Path:
    """Resolve the output folder, failing before any paid LLM call."""
    try:
        return resolve_output_dir()
    except ConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Output folder is not configured: {exc}",
        ) from exc


@app.get("/", include_in_schema=False)
async def upload_page() -> FileResponse:
    """Serve the upload page: pick a PDF, download the notebook."""
    return FileResponse(UPLOAD_PAGE, media_type="text/html")


@app.get("/health")
async def health() -> dict[str, Any]:
    """Health check."""
    return {"status": "running", "version": VERSION}


# "format: binary" makes Swagger UI (/docs) show a file picker; the OpenAPI
# 3.1 schema FastAPI emits otherwise renders as a text box.
PdfUpload = Annotated[
    UploadFile,
    File(
        description="Research paper PDF",
        json_schema_extra={"format": "binary"},
    ),
]


async def _read_pdf(file: UploadFile) -> bytes:
    """Read and validate the upload, or raise a 400."""
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only PDF files are accepted.",
        )
    content = await file.read()
    logger.info(
        "Upload received: %r (%.1f KB)", file.filename, len(content) / 1024
    )
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"File too large. Max {MAX_UPLOAD_MB} MB.",
        )
    if not content.startswith(PDF_MAGIC):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The file is not a valid PDF.",
        )
    return content


def _conversion_error(exc: Exception, provider: LLMProvider) -> HTTPException:
    """Map a failed conversion to the HTTP error returned to the client."""
    if isinstance(exc, ProviderError) and exc.retryable:
        detail = (
            f"The {provider.name} model is temporarily unavailable "
            f"(busy or rate limited). Please try again in a minute. "
            f"Details: {exc}"
        )
        error = HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=detail,
            headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
        )
    elif isinstance(exc, ProviderError):
        detail = f"The LLM request failed: {exc}"
        error = HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=detail
        )
    else:
        detail = f"The LLM returned an unusable notebook: {exc}"
        error = HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=detail
        )
    _log_failure(detail, exc, provider)
    return error


def _finish(
    result: ConvertedNotebook, output_dir: Path, started: float
) -> tuple[str, Path | None]:
    """Serialize the notebook, log it and save the server copy."""
    notebook_json = nbformat.writes(result.notebook)
    logger.info(
        "Conversion done in %.1fs: returning %r (%d cells, %.1f KB)",
        time.monotonic() - started,
        result.filename,
        len(result.notebook.cells),
        len(notebook_json) / 1024,
    )
    return notebook_json, _save_copy(
        output_dir, result.filename, notebook_json
    )


@app.post("/convert")
async def convert_pdf_to_notebook(
    file: PdfUpload,
    provider: Annotated[LLMProvider, Depends(get_llm_provider)],
    output_dir: Annotated[Path, Depends(get_output_dir)],
) -> Response:
    """Upload a paper PDF and download a Google Colab notebook (``.ipynb``).

    The notebook is named after the uploaded file: ``my_paper.pdf`` is
    returned as ``my_paper.ipynb``.

    The LLM reads the PDF directly, so layout, equations and figures are
    taken into account. Expect a conversion to take a minute or more; use
    ``/convert/stream`` to receive progress updates while it runs.
    """
    content = await _read_pdf(file)
    started = time.monotonic()
    try:
        # The SDK calls block, so run them off the event loop.
        result = await run_in_threadpool(
            partial(pdf_to_notebook, source_name=file.filename),
            content,
            provider,
        )
    except (ProviderError, ValueError) as exc:
        raise _conversion_error(exc, provider) from exc

    notebook_json, saved_path = _finish(result, output_dir, started)
    headers = {
        "Content-Disposition": f'attachment; filename="{result.filename}"'
    }
    if saved_path is not None:
        headers[SAVED_PATH_HEADER] = str(saved_path)
    return Response(
        content=notebook_json, media_type=IPYNB_MEDIA_TYPE, headers=headers
    )


def _sse(event: str, data: dict[str, Any]) -> str:
    """Format one server-sent event."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@app.post("/convert/stream")
async def convert_pdf_to_notebook_stream(
    file: PdfUpload,
    provider: Annotated[LLMProvider, Depends(get_llm_provider)],
    output_dir: Annotated[Path, Depends(get_output_dir)],
) -> StreamingResponse:
    """Like ``/convert``, but streams progress as server-sent events.

    Events (``text/event-stream``), each with a JSON ``data`` payload:

    - ``progress``: ``stage`` (``waiting``, ``thinking``, ``writing``,
      ``retrying``, ``building``, ``done``), ``message``, ``elapsed``
      seconds, ``output_chars`` and, while Claude thinks, a ``thinking``
      summary excerpt.
    - ``result``: ``filename``, ``saved_path``, ``cells``, ``elapsed`` and
      ``notebook`` (the ``.ipynb`` JSON as a string). Ends the stream.
    - ``error``: ``status`` (HTTP code it would have had) and ``detail``.
      Ends the stream.

    Upload problems (400) and missing configuration (503) are returned as
    normal HTTP errors before the stream starts. If the client disconnects,
    the conversion still finishes and is saved to the output folder.
    """
    content = await _read_pdf(file)
    started = time.monotonic()
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()

    def on_progress(progress: Progress) -> None:
        # Called from the worker thread; hand the update to the event loop.
        loop.call_soon_threadsafe(
            queue.put_nowait, ("progress", progress.to_dict())
        )

    async def convert() -> None:
        try:
            result = await run_in_threadpool(
                partial(
                    pdf_to_notebook,
                    source_name=file.filename,
                    on_progress=on_progress,
                ),
                content,
                provider,
            )
        except (ProviderError, ValueError) as exc:
            error = _conversion_error(exc, provider)
            await queue.put(
                (
                    "error",
                    {"status": error.status_code, "detail": error.detail},
                )
            )
            return
        notebook_json, saved_path = _finish(result, output_dir, started)
        await queue.put(
            (
                "result",
                {
                    "filename": result.filename,
                    "saved_path": str(saved_path) if saved_path else None,
                    "cells": len(result.notebook.cells),
                    "elapsed": round(time.monotonic() - started, 1),
                    "notebook": notebook_json,
                },
            )
        )

    # Keep a reference so the task is not garbage collected; it keeps
    # running (and saves the notebook) even if the client goes away.
    task = asyncio.create_task(convert())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    async def events() -> AsyncIterator[str]:
        yield _sse(
            "progress",
            Progress("uploaded", f"Received {file.filename}").to_dict(),
        )
        while True:
            event, data = await queue.get()
            yield _sse(event, data)
            if event in ("result", "error"):
                return

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


_background_tasks: set[asyncio.Task[None]] = set()


def _save_copy(
    directory: Path, filename: str, notebook_json: str
) -> Path | None:
    """Save the notebook on the server, so it is not lost if the browser
    does not save the download. A failure here never fails the request.
    """
    try:
        path = save_unique(directory, filename, notebook_json)
    except OSError as exc:
        logger.warning("Could not save a copy of %r: %s", filename, exc)
        return None
    logger.info("Saved a copy to %s", path)
    return path
