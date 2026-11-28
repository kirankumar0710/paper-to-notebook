"""Panels 2-4 (Upload, LLM output, Metrics): ``POST /convert`` and
``POST /convert/stream``.

A paper comes either as an uploaded PDF (``file``) or as an arXiv ID or
link (``arxiv``). For arXiv, the paper is looked up through the arXiv MCP
server and its PDF downloaded (:mod:`sources.arxiv`), so the LLM reads a
PDF either way.

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
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response, StreamingResponse

from api.dependencies import (
    get_llm_provider,
    get_output_dir,
    require_same_origin,
)
from api.routes.download import SavedNotebook, save_notebook
from api.routes.metrics import record_failure, record_success
from api.security import ConversionSlots, request_is_loopback
from api.settings import (
    IPYNB_MEDIA_TYPE,
    MAX_UPLOAD_BYTES,
    MAX_UPLOAD_MB,
    PDF_MAGIC,
    RETRY_AFTER_SECONDS,
    SAVED_PATH_HEADER,
)
from notebook_builder.converter import ConvertedNotebook, pdf_to_notebook
from notebook_builder.prompt import DEFAULT_DEPTH, Depth
from notebook_builder.verify import (
    VerifySettings,
    load_verify_settings,
    verify_allowed,
)
from providers import LLMProvider, ProviderError
from providers.base import Progress, ProgressCallback
from sources.arxiv import ArxivError, fetch_arxiv_paper, parse_arxiv_ref
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


def _binary_file_schema(schema: dict[str, Any]) -> None:
    """Describe the optional upload as a plain binary string.

    Swagger UI (/docs) shows a file picker only for ``type: string,
    format: binary``; the "file or null" schema FastAPI emits for an
    optional upload renders as a text box.
    """
    schema.pop("anyOf", None)
    schema.update(type="string", format="binary")


PdfUpload = Annotated[
    UploadFile | None,
    File(
        description="Research paper PDF. Send this or ``arxiv``.",
        json_schema_extra=_binary_file_schema,
    ),
]
ArxivRef = Annotated[
    str | None,
    Form(
        description=(
            "arXiv ID or link, e.g. 1706.03762 or "
            "https://arxiv.org/abs/1706.03762. Send this or ``file``."
        ),
    ),
]

VERIFY_HEADER = "X-Notebook-Verification"

DepthOption = Annotated[
    Depth,
    Form(
        description=(
            "Who the notebook is for: ``beginner`` (explains more, "
            "simpler code), ``practitioner`` (balanced) or ``expert`` "
            "(brief explanations, fuller implementation)."
        ),
    ),
]


def get_verify_settings(
    request: Request,
    verify: Annotated[
        bool | None,
        Form(
            description=(
                "Run the notebook with QUICK_RUN = True and let the model "
                "fix failing cells. Omit to use ``[verify] default``."
            ),
        ),
    ] = None,
) -> VerifySettings | None:
    """The ``[verify]`` settings if the run check should happen, else None.

    Raises:
        HTTPException: 403 if the check was asked for by a client that is
            not on this machine and ``[verify] allow_remote`` is false.
    """
    settings = load_verify_settings()
    wanted = settings.default if verify is None else verify
    if not wanted:
        return None
    host = request.client.host if request.client else None
    if verify_allowed(
        host, settings, host_is_local=request_is_loopback(request)
    ):
        return settings
    if verify is None:
        return None  # only the default asked for it
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=(
            "Checking that the notebook runs executes LLM-written code on "
            "the server, so it is only available from this machine "
            "(localhost). Set [verify] allow_remote = true to allow it."
        ),
    )


VerifyOption = Annotated[VerifySettings | None, Depends(get_verify_settings)]

# How many conversions may run at once ([app] max_concurrent_conversions).
CONVERSIONS = ConversionSlots()

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

    def total_usage(
        self, result: ConvertedNotebook | None = None
    ) -> dict[str, int] | None:
        """Tokens for the notebook plus any repair requests."""
        usage = dict(self.usage)
        report = result.verification if result else None
        for key, value in ((report.usage if report else None) or {}).items():
            usage[key] = usage.get(key, 0) + value
        return usage or None

    def cost(
        self,
        provider: LLMProvider,
        result: ConvertedNotebook | None = None,
    ) -> dict[str, Any] | None:
        """Estimated cost, priced at the model and speed that served it."""
        served = self.served_or_requested(provider)
        return estimate_cost(
            served["model"], self.total_usage(result), speed=served["speed"]
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
    # Read one byte past the limit, never the whole upload, so a huge file
    # is refused without being loaded into memory.
    content = await file.read(MAX_UPLOAD_BYTES + 1)
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


def _check_source(file: UploadFile | None, arxiv: str | None) -> str | None:
    """Require exactly one of ``file`` and ``arxiv``; return the arXiv ID.

    Raises:
        HTTPException: 400 if neither or both are given, or the arXiv
            reference is not valid.
    """
    has_arxiv = bool(arxiv and arxiv.strip())
    if file is not None and has_arxiv:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Send either a PDF file or an arXiv link, not both.",
        )
    if file is None and not has_arxiv:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Upload a PDF or give an arXiv ID or link.",
        )
    if not has_arxiv:
        return None
    try:
        return parse_arxiv_ref(arxiv or "")
    except ArxivError as exc:
        raise _arxiv_http_error(exc) from exc


def _arxiv_http_error(exc: ArxivError) -> HTTPException:
    """The HTTP error for a failed arXiv lookup."""
    logger.error("arXiv lookup failed (%d): %s", exc.status_code, exc)
    return HTTPException(status_code=exc.status_code, detail=str(exc))


def _upload_source(file: UploadFile) -> dict[str, Any]:
    return {"type": "upload", "filename": file.filename}


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


def _recipe(
    result: ConvertedNotebook,
    *,
    provider: LLMProvider,
    capture: _Capture,
    cost: dict[str, Any] | None,
    source: dict[str, Any],
    started: float,
) -> dict[str, Any]:
    """How a notebook was made: its inputs and the settings that produced
    it. Saved with the notebook (Download tab and the notebook's metadata);
    never includes API keys."""
    served = capture.served_or_requested(provider)
    usage = capture.total_usage(result)
    check = result.verification
    return {
        "source": source,
        "provider": provider.name,
        "model": served["model"],
        "speed": served["speed"],
        "options": dict(provider.settings.options),
        "depth": result.depth,
        "run_check": (
            {"status": check.status, "repairs": check.repairs}
            if check
            else None
        ),
        "elapsed": round(time.monotonic() - started, 1),
        "total_tokens": usage.get("total_tokens") if usage else None,
        "cost_usd": cost["total_usd"] if cost else None,
    }


def _finish(
    result: ConvertedNotebook,
    output_dir: Path,
    started: float,
    recipe: dict[str, Any],
) -> tuple[str, SavedNotebook | None]:
    """Record the recipe in the notebook, serialize it, log it and save the
    server copy."""
    result.notebook.metadata.setdefault("paper_to_notebook", {}).update(recipe)
    notebook_json = nbformat.writes(result.notebook)
    logger.info(
        "Conversion done in %.1fs: returning %r (%d cells, %.1f KB)",
        time.monotonic() - started,
        result.filename,
        len(result.notebook.cells),
        len(notebook_json) / 1024,
    )
    saved = save_notebook(
        output_dir, result.filename, result.title, notebook_json, recipe
    )
    return notebook_json, saved


@router.post("/convert", dependencies=[Depends(require_same_origin)])
async def convert_pdf_to_notebook(
    provider: Annotated[LLMProvider, Depends(get_llm_provider)],
    output_dir: Annotated[Path, Depends(get_output_dir)],
    file: PdfUpload = None,
    arxiv: ArxivRef = None,
    verify: VerifyOption = None,
    depth: DepthOption = DEFAULT_DEPTH,
) -> Response:
    """Upload a paper PDF, or give an arXiv ID or link, and download a
    Google Colab notebook (``.ipynb``).

    The notebook is named after the uploaded file (``my_paper.pdf`` is
    returned as ``my_paper.ipynb``), or after the arXiv ID and title.
    An arXiv paper is looked up through the arXiv MCP server and its PDF
    downloaded from arxiv.org.

    With ``verify=true`` the notebook is run with ``QUICK_RUN = True``
    before it is returned, and the model fixes cells that fail; the
    ``X-Notebook-Verification`` header gives the result.

    ``depth`` (``beginner``, ``practitioner`` or ``expert``) sets how
    much the notebook explains compared with how much it implements.

    At most ``[app] max_concurrent_conversions`` conversions run at once;
    more get 429 with ``Retry-After``.

    The LLM reads the PDF directly, so layout, equations and figures are
    taken into account. Expect a conversion to take a minute or more; use
    ``/convert/stream`` to receive progress updates while it runs.
    """
    arxiv_id = _check_source(file, arxiv)
    upload: bytes | None = None
    if arxiv_id is None:
        assert file is not None  # noqa: S101 (type narrowing)
        upload = await _read_pdf(file)
    CONVERSIONS.acquire()  # 429 if too many are running
    try:
        return await _convert_once(
            provider,
            output_dir,
            file=file,
            arxiv_id=arxiv_id,
            upload=upload,
            verify=verify,
            depth=depth,
        )
    finally:
        CONVERSIONS.release()


async def _convert_once(
    provider: LLMProvider,
    output_dir: Path,
    *,
    file: UploadFile | None,
    arxiv_id: str | None,
    upload: bytes | None,
    verify: VerifySettings | None,
    depth: str,
) -> Response:
    """One ``/convert`` conversion, run while holding a slot."""
    if upload is None:
        try:
            paper = await fetch_arxiv_paper(
                arxiv_id or "", max_bytes=MAX_UPLOAD_BYTES
            )
        except ArxivError as exc:
            raise _arxiv_http_error(exc) from exc
        content, source_name = paper.pdf, paper.source_name
        source = paper.describe()
    else:
        assert file is not None  # noqa: S101 (type narrowing)
        content, source_name = upload, file.filename
        source = _upload_source(file)
    started = time.monotonic()
    capture = _Capture()
    try:
        # The SDK calls block, so run them off the event loop.
        result = await run_in_threadpool(
            partial(
                pdf_to_notebook,
                source_name=source_name,
                on_progress=capture,
                verify=verify,
                depth=depth,
            ),
            content,
            provider,
        )
    except (ProviderError, ValueError) as exc:
        record_failure()
        raise _conversion_error(exc, provider) from exc

    cost = capture.cost(provider, result)
    record_success(capture.total_usage(result), cost)
    recipe = _recipe(
        result,
        provider=provider,
        capture=capture,
        cost=cost,
        source=source,
        started=started,
    )
    notebook_json, saved = _finish(result, output_dir, started, recipe)
    headers = {
        "Content-Disposition": f'attachment; filename="{result.filename}"'
    }
    if saved is not None:
        headers[SAVED_PATH_HEADER] = str(saved.path)
    if result.verification is not None:
        headers[VERIFY_HEADER] = result.verification.status
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
    source: dict[str, Any],
) -> dict[str, Any]:
    """The ``result`` event: the notebook plus the dashboard's metrics."""
    kinds = [cell.cell_type for cell in result.notebook.cells]
    usage = capture.total_usage(result)
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
        "source": source,
        "upload_bytes": upload_bytes,
        "pages": pages,
        "notebook_bytes": len(notebook_json.encode()),
        "code_lines": result.code_lines,
        "pip_packages": list(result.pip_packages),
        "dropped_packages": list(result.dropped_packages),
        "requires_gpu": result.requires_gpu,
        "depth": result.depth,
        "verification": (
            result.verification.to_dict() if result.verification else None
        ),
        "session": session,
        "notebook": notebook_json,
    }


@router.post("/convert/stream", dependencies=[Depends(require_same_origin)])
async def convert_pdf_to_notebook_stream(
    provider: Annotated[LLMProvider, Depends(get_llm_provider)],
    output_dir: Annotated[Path, Depends(get_output_dir)],
    file: PdfUpload = None,
    arxiv: ArxivRef = None,
    verify: VerifyOption = None,
    depth: DepthOption = DEFAULT_DEPTH,
) -> StreamingResponse:
    """Like ``/convert``, but streams progress as server-sent events.

    Events (``text/event-stream``), each with a JSON ``data`` payload:

    - ``progress``: ``stage`` (``uploaded``, ``fetching`` while an arXiv
      paper is looked up and downloaded, ``waiting``, ``thinking``,
      ``writing``,
      ``retrying``, ``verifying`` and ``repairing`` (only with
      ``verify``), ``building``, ``done``), ``message``, ``elapsed``
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
      ``requires_gpu``, ``depth``, ``verification`` (null, or ``status``
      ``passed``,
      ``failed``, ``skipped`` or ``error``, with ``message``,
      ``code_cells``, ``failed_cell``, ``error``, ``repairs``,
      ``repaired_cells``, ``elapsed`` and the repair ``usage``),
      ``source`` (``type`` ``upload`` with
      ``filename``, or ``arxiv`` with ``paper_id``, ``title``,
      ``authors``, ``published``, ``abs_url`` and ``pdf_url``),
      ``session`` (totals since the server started, as
      ``/api/metrics/session``) and ``notebook`` (the ``.ipynb`` JSON as a
      string). Ends the stream.
    - ``error``: ``status`` (HTTP code it would have had) and ``detail``.
      Ends the stream.

    Upload problems and invalid arXiv references (400), too many running
    conversions (429) and missing configuration (503) are returned as
    normal HTTP errors before the
    stream starts; a failed arXiv lookup is an ``error`` event. If the
    client disconnects, the conversion still finishes and is saved to the
    output folder.
    """
    arxiv_id = _check_source(file, arxiv)
    upload: bytes | None = None
    if arxiv_id is None:
        assert file is not None  # noqa: S101 (type narrowing)
        upload = await _read_pdf(file)
    started = time.monotonic()
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()

    def send_progress(progress: Progress) -> None:
        # Called from the worker thread; hand the update to the event loop.
        loop.call_soon_threadsafe(
            queue.put_nowait, ("progress", progress.to_dict())
        )

    capture = _Capture(forward=send_progress)

    async def paper() -> tuple[bytes, str, dict[str, Any]]:
        """The PDF, its name and its source: the upload, or from arXiv."""
        if upload is not None:
            assert file is not None  # noqa: S101 (type narrowing)
            return upload, file.filename or "paper.pdf", _upload_source(file)
        fetched = await fetch_arxiv_paper(
            arxiv_id or "",
            max_bytes=MAX_UPLOAD_BYTES,
            notify=lambda message: queue.put_nowait(
                ("progress", Progress("fetching", message).to_dict())
            ),
        )
        return fetched.pdf, fetched.source_name, fetched.describe()

    async def convert() -> None:
        try:
            content, source_name, source = await paper()
        except ArxivError as exc:
            record_failure()
            error = _arxiv_http_error(exc)
            await queue.put(
                (
                    "error",
                    {"status": error.status_code, "detail": error.detail},
                )
            )
            return
        pages = await run_in_threadpool(count_pages, content)
        try:
            result = await run_in_threadpool(
                partial(
                    pdf_to_notebook,
                    source_name=source_name,
                    on_progress=capture,
                    verify=verify,
                    depth=depth,
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
        cost = capture.cost(provider, result)
        session = record_success(capture.total_usage(result), cost)
        recipe = _recipe(
            result,
            provider=provider,
            capture=capture,
            cost=cost,
            source=source,
            started=started,
        )
        notebook_json, saved = _finish(result, output_dir, started, recipe)
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
            source=source,
        )
        await queue.put(("result", event))

    CONVERSIONS.acquire()  # 429 if too many are running
    task = asyncio.create_task(convert())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    # The slot is held until the conversion ends, even if the client
    # disconnects first.
    task.add_done_callback(lambda _: CONVERSIONS.release())

    async def events() -> AsyncIterator[str]:
        if file is not None:
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
