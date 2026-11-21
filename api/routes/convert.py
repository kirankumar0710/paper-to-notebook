"""Panels 2-4 (Upload, LLM output, Metrics): ``POST /convert`` and
``POST /convert/stream``.

The dashboard uses the stream: its ``progress`` events drive the LLM output
panel, and the final ``result`` event carries the metrics and the notebook.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from functools import partial
from pathlib import Path
from typing import Annotated, Any

import nbformat
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response, StreamingResponse

from api.dependencies import get_llm_provider, get_output_dir
from api.routes.download import SavedNotebook, save_notebook
from api.routes.metrics import record_failure, record_success
from api.settings import (
    IPYNB_MEDIA_TYPE,
    MAX_UPLOAD_BYTES,
    MAX_UPLOAD_MB,
    PDF_MAGIC,
    RETRY_AFTER_SECONDS,
    SAVED_PATH_HEADER,
)
from notebook_builder.converter import ConvertedNotebook, pdf_to_notebook
from providers import LLMProvider, ProviderError
from providers.base import Progress, ProgressCallback
from util.log import get_logger
from util.pdf import count_pages
from util.pricing import estimate_cost

__all__ = [
    "convert_pdf_to_notebook",
    "convert_pdf_to_notebook_stream",
    "router",
]

logger = get_logger(__name__)
router = APIRouter()

# "format: binary" makes Swagger UI (/docs) show a file picker; the OpenAPI
# 3.1 schema FastAPI emits otherwise renders as a text box.
PdfUpload = Annotated[
    UploadFile,
    File(
        description="Research paper PDF",
        json_schema_extra={"format": "binary"},
    ),
]

# Running conversions. Keeping a reference stops them being garbage
# collected; they finish (and save the notebook) even if the client leaves.
_background_tasks: set[asyncio.Task[None]] = set()


class _Capture:
    """Progress callback that remembers token usage and what served the
    request, and optionally forwards each update."""

    def __init__(self, forward: ProgressCallback | None = None) -> None:
        self.usage: dict[str, int] = {}
        self.served: dict[str, Any] | None = None
        self.forward = forward

    def __call__(self, progress: Progress) -> None:
        if progress.usage:
            self.usage.update(progress.usage)
        if progress.served:
            self.served = progress.served
        if self.forward is not None:
            self.forward(progress)

    def served_or_requested(self, provider: LLMProvider) -> dict[str, Any]:
        """What served the request, or what was asked for if not reported."""
        return self.served or {
            "model": provider.model,
            "speed": provider.requested_speed,
            "model_fallback": False,
            "speed_fallback": False,
        }

    def cost(self, provider: LLMProvider) -> dict[str, Any] | None:
        """Estimated cost, priced at the model and speed that served it."""
        served = self.served_or_requested(provider)
        return estimate_cost(
            served["model"], self.usage or None, speed=served["speed"]
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
            f"The {provider.name} model is busy or rate limited. Please "
            f"try again in a minute; if it keeps failing, check your "
            f"account's rate limits and quota. Details: {exc}"
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
) -> tuple[str, SavedNotebook | None]:
    """Serialize the notebook, log it and save the server copy."""
    notebook_json = nbformat.writes(result.notebook)
    logger.info(
        "Conversion done in %.1fs: returning %r (%d cells, %.1f KB)",
        time.monotonic() - started,
        result.filename,
        len(result.notebook.cells),
        len(notebook_json) / 1024,
    )
    saved = save_notebook(
        output_dir, result.filename, result.title, notebook_json
    )
    return notebook_json, saved


@router.post("/convert")
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
    capture = _Capture()
    try:
        # The SDK calls block, so run them off the event loop.
        result = await run_in_threadpool(
            partial(
                pdf_to_notebook, source_name=file.filename, on_progress=capture
            ),
            content,
            provider,
        )
    except (ProviderError, ValueError) as exc:
        record_failure()
        raise _conversion_error(exc, provider) from exc

    record_success(capture.usage or None, capture.cost(provider))
    notebook_json, saved = _finish(result, output_dir, started)
    headers = {
        "Content-Disposition": f'attachment; filename="{result.filename}"'
    }
    if saved is not None:
        headers[SAVED_PATH_HEADER] = str(saved.path)
    return Response(
        content=notebook_json, media_type=IPYNB_MEDIA_TYPE, headers=headers
    )


def _sse(event: str, data: dict[str, Any]) -> str:
    """Format one server-sent event."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _result_event(
    result: ConvertedNotebook,
    notebook_json: str,
    saved: SavedNotebook | None,
    *,
    provider: LLMProvider,
    capture: _Capture,
    cost: dict[str, Any] | None,
    session: dict[str, Any],
    upload_bytes: int,
    pages: int | None,
    started: float,
) -> dict[str, Any]:
    """The ``result`` event: the notebook plus the dashboard's metrics."""
    kinds = [cell.cell_type for cell in result.notebook.cells]
    usage = capture.usage
    return {
        "filename": result.filename,
        "title": result.title,
        "saved_path": str(saved.path) if saved else None,
        "download_url": saved.download_url if saved else None,
        "cells": len(kinds),
        "markdown_cells": kinds.count("markdown"),
        "code_cells": kinds.count("code"),
        "elapsed": round(time.monotonic() - started, 1),
        "provider": provider.name,
        "model": provider.model,
        "requested_speed": provider.requested_speed,
        "served": capture.served_or_requested(provider),
        "usage": usage or None,
        "max_output_tokens": provider.max_output_tokens,
        "cost": cost,
        "upload_bytes": upload_bytes,
        "pages": pages,
        "notebook_bytes": len(notebook_json.encode()),
        "code_lines": result.code_lines,
        "pip_packages": list(result.pip_packages),
        "dropped_packages": list(result.dropped_packages),
        "requires_gpu": result.requires_gpu,
        "session": session,
        "notebook": notebook_json,
    }


@router.post("/convert/stream")
async def convert_pdf_to_notebook_stream(
    file: PdfUpload,
    provider: Annotated[LLMProvider, Depends(get_llm_provider)],
    output_dir: Annotated[Path, Depends(get_output_dir)],
) -> StreamingResponse:
    """Like ``/convert``, but streams progress as server-sent events.

    Events (``text/event-stream``), each with a JSON ``data`` payload:

    - ``progress``: ``stage`` (``waiting``, ``thinking``, ``writing``,
      ``retrying``, ``building``, ``done``), ``message``, ``elapsed``
      seconds, ``output_chars``, while Claude thinks a ``thinking``
      summary excerpt and ``thinking_delta`` (the new summary text since
      the previous event), and on ``done`` the token ``usage`` if known.
    - ``result``: ``filename``, ``title``, ``saved_path``, ``download_url``
      (``/api/notebooks/{id}``, or null if saving failed), ``cells``,
      ``markdown_cells``, ``code_cells``, ``elapsed``, ``provider``,
      ``model`` and ``requested_speed`` (what was asked for), ``served``
      (``model``, ``speed``, ``model_fallback``, ``speed_fallback``: what
      actually answered), ``usage`` (``input_tokens``, ``output_tokens``,
      ``total_tokens``, or null), ``max_output_tokens`` (the output budget,
      or null), ``cost`` (estimated USD from ``[pricing]`` in config.toml,
      or null), ``upload_bytes``, ``pages`` (or null), ``notebook_bytes``,
      ``code_lines``, ``pip_packages``, ``dropped_packages``,
      ``requires_gpu``, ``session`` (totals since the server started, as
      ``/api/metrics/session``) and ``notebook`` (the ``.ipynb`` JSON as a
      string). Ends the stream.
    - ``error``: ``status`` (HTTP code it would have had) and ``detail``.
      Ends the stream.

    Upload problems (400) and missing configuration (503) are returned as
    normal HTTP errors before the stream starts. If the client disconnects,
    the conversion still finishes and is saved to the output folder.
    """
    content = await _read_pdf(file)
    started = time.monotonic()
    pages = await run_in_threadpool(count_pages, content)
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()

    def send_progress(progress: Progress) -> None:
        # Called from the worker thread; hand the update to the event loop.
        loop.call_soon_threadsafe(
            queue.put_nowait, ("progress", progress.to_dict())
        )

    capture = _Capture(forward=send_progress)

    async def convert() -> None:
        try:
            result = await run_in_threadpool(
                partial(
                    pdf_to_notebook,
                    source_name=file.filename,
                    on_progress=capture,
                ),
                content,
                provider,
            )
        except (ProviderError, ValueError) as exc:
            record_failure()
            error = _conversion_error(exc, provider)
            await queue.put(
                (
                    "error",
                    {"status": error.status_code, "detail": error.detail},
                )
            )
            return
        cost = capture.cost(provider)
        session = record_success(capture.usage or None, cost)
        notebook_json, saved = _finish(result, output_dir, started)
        event = _result_event(
            result,
            notebook_json,
            saved,
            provider=provider,
            capture=capture,
            cost=cost,
            session=session,
            upload_bytes=len(content),
            pages=pages,
            started=started,
        )
        await queue.put(("result", event))

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
