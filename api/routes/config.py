"""Panel 1 (Configuration): ``GET /api/config``, plus ``GET /health``."""

from __future__ import annotations

import os
from functools import partial
from typing import Any

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool

from api.dependencies import check_configuration
from api.security import request_is_loopback
from api.settings import MAX_UPLOAD_MB, VERSION
from notebook_builder.verify import load_verify_settings, verify_allowed
from providers.factory import resolve_settings
from util.config import ConfigError, locate_output_dir

__all__ = ["describe_configuration", "router"]

router = APIRouter()


def describe_configuration(
    client_host: str | None = None, *, host_is_local: bool = True
) -> dict[str, Any]:
    """Summarise the current settings for the dashboard.

    Never includes the API key itself, only where it was (or was not)
    found. Missing settings are reported in ``problems`` instead of
    raising.

    Args:
        client_host: The browser's address; the run check is offered only
            to this machine unless ``[verify] allow_remote`` is set.
    """
    info: dict[str, Any] = {
        "provider": None,
        "model": None,
        "base_url": None,
        "options": {},
        "api_key": {"found": False, "source": None, "env_vars": []},
        "output_dir": {
            "path": None,
            "source": None,
            "exists": False,
            "writable": False,
        },
        "max_upload_mb": MAX_UPLOAD_MB,
        "verify": _describe_verify(client_host, host_is_local),
        "problems": check_configuration(),
    }
    try:
        settings = resolve_settings(require_key=False, require_model=False)
    except (ConfigError, FileNotFoundError):
        pass
    else:
        info.update(
            provider=settings.name,
            model=settings.model,
            base_url=settings.base_url,
            options=settings.options,
            api_key={
                "found": bool(settings.api_key),
                "source": settings.api_key_source,
                "env_vars": list(settings.api_key_env),
            },
        )
    try:
        path, source = locate_output_dir()
    except (ConfigError, FileNotFoundError):
        pass
    else:
        exists = path.is_dir()
        info["output_dir"] = {
            "path": str(path),
            "source": source,
            "exists": exists,
            # A missing folder is created on the first save.
            "writable": os.access(path, os.W_OK) if exists else True,
        }
    return info


def _describe_verify(
    client_host: str | None, host_is_local: bool = True
) -> dict[str, Any]:
    """Whether the dashboard may offer the run check, and its defaults."""
    try:
        settings = load_verify_settings()
    except (ConfigError, FileNotFoundError):
        return {"available": False, "default": False, "reason": None}
    available = verify_allowed(
        client_host, settings, host_is_local=host_is_local
    )
    return {
        "available": available,
        "default": settings.default and available,
        "max_repairs": settings.max_repairs,
        "cell_timeout": settings.cell_timeout,
        "reason": None
        if available
        else (
            "Only available from this machine: it runs LLM-written code "
            "on the server."
        ),
    }


@router.get("/api/config")
async def config(request: Request) -> dict[str, Any]:
    """Current provider, API key status and output folder (no secrets)."""
    host = request.client.host if request.client else None
    return await run_in_threadpool(
        partial(
            describe_configuration,
            host,
            host_is_local=request_is_loopback(request),
        )
    )


@router.get("/health")
async def health() -> dict[str, Any]:
    """Health check."""
    return {"status": "running", "version": VERSION}
