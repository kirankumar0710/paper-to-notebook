"""Configuration checks and FastAPI dependencies shared by the routes."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, status

from providers import LLMProvider, ProviderError, get_provider
from providers.factory import resolve_settings
from util.config import ConfigError, resolve_output_dir

__all__ = [
    "check_configuration",
    "get_llm_provider",
    "get_output_dir",
    "require_same_origin",
]

# Sec-Fetch-Site values a browser sends for requests the dashboard itself
# makes ("same-origin") or the user starts directly ("none").
_ALLOWED_FETCH_SITES = frozenset({"same-origin", "none"})


def require_same_origin(request: Request) -> None:
    """Refuse requests that another website makes from the user's browser.

    A page on any site can send a form POST to this server (e.g. at
    localhost:8000) from a visitor's browser, which would start a paid
    conversion and, with the run check, execute LLM-written code on this
    machine. Browsers mark such requests with ``Sec-Fetch-Site`` and
    ``Origin``, which pages cannot forge. Requests without either header
    (curl, scripts, tests) are not from a web page and are allowed.

    Raises:
        HTTPException: 403 for a request from another site.
    """
    site = request.headers.get("sec-fetch-site")
    if site is not None:
        if site in _ALLOWED_FETCH_SITES:
            return
        _refuse(f"Sec-Fetch-Site: {site}")
    origin = request.headers.get("origin")
    if origin is None:
        return
    host = request.headers.get("host", "")
    if origin == "null" or urlsplit(origin).netloc != host:
        _refuse(f"Origin: {origin}")


def _refuse(reason: str) -> None:
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=(
            "Requests from other websites are not allowed "
            f"({reason}). Use the dashboard or call the API directly."
        ),
    )


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
