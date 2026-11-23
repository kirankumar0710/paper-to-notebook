"""Fetch an arXiv paper's PDF, looking it up through the arXiv MCP server.

The lookup runs the ``arxiv-mcp-server`` package as an MCP server over
stdio (one short-lived process per lookup) and calls its ``get_abstract``
tool, which returns the title, authors, abstract and PDF URL. The app then
downloads the PDF itself, so the LLM reads the same PDF it would get from
an upload; the server's own text extraction is not used.

Every step is recorded in :data:`sources.mcp_log.MCP_LOG` for the
dashboard's MCP activity log.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from sources.mcp_log import MCP_LOG, McpLog
from util.config import Config, ConfigError, load_config
from util.log import get_logger

__all__ = [
    "ArxivError",
    "ArxivPaper",
    "ArxivSettings",
    "fetch_arxiv_paper",
    "load_arxiv_settings",
    "parse_arxiv_ref",
]

TOOL = "get_abstract"
DEFAULT_COMMAND = ("{python}", "-m", "arxiv_mcp_server")
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_PDF_BYTES = 15 * 1024 * 1024
PDF_MAGIC = b"%PDF-"
# A generic client name: no personal details go to arXiv.
USER_AGENT = "paper-to-notebook (arXiv PDF download)"
ARXIV_HOSTS = ("arxiv.org", "www.arxiv.org", "export.arxiv.org")

# New-style IDs (2017 on: 1706.03762, 2401.12345v2) and old-style ones
# (hep-th/9901001, math.GT/0309136).
_NEW_ID = r"\d{4}\.\d{4,5}(?:v\d+)?"
_OLD_ID = r"[a-z]+(?:-[a-z]+)*(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?"
_ARXIV_ID = re.compile(rf"(?:{_NEW_ID}|{_OLD_ID})")
_URL_PATH = re.compile(r"^/(?:abs|pdf|html)/(?P<id>.+?)(?:\.pdf)?/?$")

logger = get_logger(__name__)

Notify = Callable[[str], None]


class ArxivError(Exception):
    """An arXiv lookup failed. ``status_code`` is the HTTP status to return."""

    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class ArxivSettings:
    """How to start the MCP server, from ``[arxiv]`` in ``config.toml``."""

    command: tuple[str, ...] = DEFAULT_COMMAND
    timeout: float = DEFAULT_TIMEOUT_SECONDS

    def argv(self) -> list[str]:
        """The command with ``{python}`` replaced by this interpreter."""
        return [
            part.replace("{python}", sys.executable) for part in self.command
        ]


@dataclass(frozen=True)
class ArxivPaper:
    """A paper found on arXiv, with its PDF."""

    paper_id: str
    title: str
    authors: tuple[str, ...]
    abstract: str
    published: str | None
    pdf_url: str
    pdf: bytes

    @property
    def source_name(self) -> str:
        """Name to give the notebook, e.g. ``arXiv 1706.03762 Attention…``."""
        return f"arXiv {self.paper_id} {self.title}.pdf"

    def describe(self) -> dict[str, Any]:
        """Metadata for the dashboard (no PDF bytes)."""
        return {
            "type": "arxiv",
            "paper_id": self.paper_id,
            "title": self.title,
            "authors": list(self.authors),
            "published": self.published,
            "abs_url": f"https://arxiv.org/abs/{self.paper_id}",
            "pdf_url": self.pdf_url,
        }


def load_arxiv_settings(config: Config | None = None) -> ArxivSettings:
    """Read ``[arxiv]`` from the config, with defaults for missing keys.

    Raises:
        ConfigError: If ``command`` is not a list of strings or ``timeout``
            is not a positive number.
    """
    if config is None:
        config = load_config()
    section = config.get("arxiv", {})
    command = section.get("command", list(DEFAULT_COMMAND))
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(part, str) and part for part in command)
    ):
        raise ConfigError(
            "[arxiv] command must be a list of strings, e.g. "
            '["{python}", "-m", "arxiv_mcp_server"].'
        )
    timeout = section.get("timeout", DEFAULT_TIMEOUT_SECONDS)
    if isinstance(timeout, bool) or not isinstance(timeout, int | float):
        raise ConfigError("[arxiv] timeout must be a number of seconds.")
    if timeout <= 0:
        raise ConfigError("[arxiv] timeout must be greater than 0.")
    return ArxivSettings(command=tuple(command), timeout=float(timeout))


def parse_arxiv_ref(text: str) -> str:
    """Return the arXiv ID in an ID, ``arXiv:`` reference or arxiv.org URL.

    Accepts e.g. ``1706.03762``, ``arXiv:1706.03762v7``,
    ``https://arxiv.org/abs/1706.03762``, ``arxiv.org/pdf/1706.03762.pdf``
    and old-style IDs such as ``hep-th/9901001``.

    Raises:
        ArxivError: (400) If no arXiv ID can be found.
    """
    ref = (text or "").strip()
    if ref.lower().startswith("arxiv:"):
        ref = ref[6:].strip()
    if "arxiv.org" in ref.lower():
        url = urlparse(ref if "://" in ref else f"https://{ref}")
        match = _URL_PATH.match(url.path)
        if (url.hostname or "").lower() not in ARXIV_HOSTS or not match:
            raise ArxivError(
                "That is not an arXiv paper link. Use a link like "
                "https://arxiv.org/abs/1706.03762, or just the ID.",
                status_code=400,
            )
        ref = match.group("id")
    if not _ARXIV_ID.fullmatch(ref):
        raise ArxivError(
            f"{text.strip()!r} is not an arXiv ID or link. Use an ID like "
            "1706.03762 or a link like https://arxiv.org/abs/1706.03762.",
            status_code=400,
        )
    return ref


@asynccontextmanager
async def _stdio_session(argv: list[str]) -> AsyncIterator[Any]:
    """Start the MCP server over stdio and yield a connected session."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    # Papers the server caches go to a temp folder, not the project. The
    # MCP SDK passes only a minimal environment (PATH, HOME, …) to the
    # server process, so API keys are not shared with it.
    storage = tempfile.mkdtemp(prefix="paper-to-notebook-arxiv-")
    params = StdioServerParameters(
        command=argv[0], args=[*argv[1:], "--storage-path", storage]
    )
    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as session,
    ):
        yield session


def _find(exc: BaseException, kind: type[Exception]) -> Exception | None:
    """Find an exception of ``kind`` inside (possibly grouped) ``exc``."""
    if isinstance(exc, kind):
        return exc
    if isinstance(exc, BaseExceptionGroup):
        for inner in exc.exceptions:
            found = _find(inner, kind)
            if found is not None:
                return found
    return None


def _tool_text(result: Any) -> str:
    """The text parts of an MCP tool result, joined."""
    return "\n".join(
        part.text
        for part in getattr(result, "content", None) or []
        if getattr(part, "type", None) == "text"
    )


def _parse_abstract(result: Any, paper_id: str) -> dict[str, Any]:
    """Check a ``get_abstract`` result and return its JSON payload."""
    text = _tool_text(result)
    if getattr(result, "isError", False):
        raise ArxivError(f"The arXiv MCP server reported an error: {text}")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ArxivError(
            "The arXiv MCP server returned something that is not JSON."
        ) from exc
    if not isinstance(data, dict):
        raise ArxivError("The arXiv MCP server returned an unexpected reply.")
    if data.get("status") != "success":
        message = str(data.get("message") or "unknown error")
        status_code = 404 if "not found" in message.lower() else 502
        raise ArxivError(f"arXiv lookup failed: {message}", status_code)
    if not str(data.get("title") or "").strip():
        raise ArxivError(f"arXiv returned no title for {paper_id}.")
    return data


def _safe_pdf_url(url: Any, paper_id: str) -> str:
    """Use the PDF URL from arXiv only if it points at arxiv.org.

    The MCP server's reply is external data, so the app never downloads
    from a host it names other than arXiv's own.
    """
    fallback = f"https://arxiv.org/pdf/{paper_id}"
    if not isinstance(url, str):
        return fallback
    parsed = urlparse(url)
    if (
        parsed.scheme not in ("http", "https")
        or (parsed.hostname or "").lower() not in ARXIV_HOSTS
    ):
        return fallback
    return parsed._replace(scheme="https").geturl()


async def _lookup(
    paper_id: str,
    *,
    settings: ArxivSettings,
    session_factory: Callable[[], AbstractAsyncContextManager[Any]],
    log: McpLog,
    run: str,
) -> dict[str, Any]:
    """Run the MCP part: connect, list tools, call ``get_abstract``."""
    log.add(
        "server",
        "Starting the arXiv MCP server (stdio)",
        run=run,
        detail=" ".join(settings.command),
    )
    started = time.monotonic()
    async with session_factory() as session:
        init = await session.initialize()
        info = getattr(init, "serverInfo", None)
        log.add(
            "server",
            "Connected to "
            f"{getattr(info, 'name', 'MCP server')} "
            f"{getattr(info, 'version', '')}".rstrip(),
            run=run,
            duration_ms=(time.monotonic() - started) * 1000,
        )
        tools = [tool.name for tool in (await session.list_tools()).tools]
        log.add(
            "server",
            f"{len(tools)} tools available",
            run=run,
            detail=", ".join(tools),
        )
        if TOOL not in tools:
            raise ArxivError(f"The MCP server has no {TOOL} tool.")
        arguments = {"paper_id": paper_id}
        log.add(
            "call",
            f"{TOOL}({json.dumps(arguments)})",
            run=run,
            tool=TOOL,
            arguments=arguments,
        )
        called = time.monotonic()
        result = await session.call_tool(TOOL, arguments)
        elapsed_ms = (time.monotonic() - called) * 1000
        data = _parse_abstract(result, paper_id)
        authors = data.get("authors") or []
        log.add(
            "result",
            f"{TOOL} → {data['title']}",
            run=run,
            tool=TOOL,
            duration_ms=elapsed_ms,
            detail=(
                f"{len(authors)} authors · published "
                f"{data.get('published') or 'unknown'} · "
                f"pdf_url {data.get('pdf_url') or 'not given'}"
            ),
        )
    log.add("server", "MCP server stopped", run=run)
    return data


async def _download_pdf(
    url: str,
    *,
    client: httpx.AsyncClient | None,
    max_bytes: int,
    log: McpLog,
    run: str,
) -> bytes:
    """Download the PDF, refusing anything too large or not a PDF."""
    started = time.monotonic()
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(60.0, connect=15.0),
            headers={"User-Agent": USER_AGENT},
        )
    chunks: list[bytes] = []
    size = 0
    try:
        async with client.stream("GET", url) as response:
            if response.status_code != 200:
                raise ArxivError(
                    f"arXiv returned HTTP {response.status_code} for {url}.",
                    404 if response.status_code == 404 else 502,
                )
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    raise ArxivError(
                        f"The PDF is larger than the "
                        f"{max_bytes // (1024 * 1024)} MB limit.",
                        status_code=400,
                    )
                chunks.append(chunk)
    except httpx.HTTPError as exc:
        raise ArxivError(f"Could not download the PDF: {exc}") from exc
    finally:
        if owns_client:
            await client.aclose()
    content = b"".join(chunks)
    if not content.startswith(PDF_MAGIC):
        raise ArxivError(f"{url} did not return a PDF.")
    log.add(
        "http",
        f"GET {url} → 200 · {size / 1024:.0f} KB",
        run=run,
        duration_ms=(time.monotonic() - started) * 1000,
        detail="Downloaded by the app, not an MCP tool: the LLM reads "
        "this PDF directly.",
    )
    return content


async def fetch_arxiv_paper(
    ref: str,
    *,
    settings: ArxivSettings | None = None,
    log: McpLog = MCP_LOG,
    notify: Notify | None = None,
    max_bytes: int = DEFAULT_MAX_PDF_BYTES,
    session_factory: Callable[[], AbstractAsyncContextManager[Any]]
    | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> ArxivPaper:
    """Look a paper up through the arXiv MCP server and download its PDF.

    Args:
        ref: An arXiv ID or link (see :func:`parse_arxiv_ref`).
        settings: How to start the server. Defaults to ``[arxiv]`` in the
            config.
        log: Where to record each step.
        notify: Called with a short status line before each step.
        max_bytes: Largest PDF accepted.
        session_factory: Returns an async context manager yielding an MCP
            client session (for tests). Defaults to the stdio server.
        http_client: Client for the PDF download (for tests).

    Raises:
        ArxivError: With an HTTP ``status_code``: 400 for a bad reference or
            a PDF that is too large, 404 if arXiv has no such paper, 504 on
            a timeout, otherwise 502.
    """
    paper_id = parse_arxiv_ref(ref)
    run = f"arXiv {paper_id}"
    settings = settings or load_arxiv_settings()
    if session_factory is None:
        argv = settings.argv()

        def session_factory() -> AbstractAsyncContextManager[Any]:
            return _stdio_session(argv)

    def say(message: str) -> None:
        logger.info("%s: %s", run, message)
        if notify is not None:
            notify(message)

    say(f"Looking up arXiv {paper_id} with the arXiv MCP server…")
    try:
        async with asyncio.timeout(settings.timeout):
            data = await _lookup(
                paper_id,
                settings=settings,
                session_factory=session_factory,
                log=log,
                run=run,
            )
    except TimeoutError as exc:
        message = (
            f"The arXiv MCP server did not answer within "
            f"{settings.timeout:.0f} seconds."
        )
        log.add("error", message, run=run)
        raise ArxivError(message, status_code=504) from exc
    except Exception as exc:  # noqa: BLE001 - reported to the user below
        error = _find(exc, ArxivError)
        if error is None:
            error = ArxivError(
                f"Could not use the arXiv MCP server: "
                f"{type(exc).__name__}: {exc}"
            )
            error.__cause__ = exc
        log.add("error", str(error), run=run)
        raise error from exc

    pdf_url = _safe_pdf_url(data.get("pdf_url"), paper_id)
    say(f"Found “{data['title']}”. Downloading the PDF…")
    try:
        pdf = await _download_pdf(
            pdf_url,
            client=http_client,
            max_bytes=max_bytes,
            log=log,
            run=run,
        )
    except ArxivError as exc:
        log.add("error", str(exc), run=run)
        raise
    return ArxivPaper(
        paper_id=paper_id,
        title=str(data["title"]).strip(),
        authors=tuple(str(a) for a in data.get("authors") or []),
        abstract=str(data.get("abstract") or ""),
        published=data.get("published"),
        pdf_url=pdf_url,
        pdf=pdf,
    )
