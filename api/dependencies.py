"""Configuration checks and FastAPI dependencies shared by the routes."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from fastapi import HTTPException, status

from providers import LLMProvider, ProviderError, get_provider
from providers.factory import resolve_settings
from util.config import ConfigError, resolve_output_dir

__all__ = ["check_configuration", "get_llm_provider", "get_output_dir"]


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
