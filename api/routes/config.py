"""Panel 1 (Configuration): ``GET /api/config``, plus ``GET /health``."""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool

from api.dependencies import check_configuration
from api.settings import MAX_UPLOAD_MB, VERSION
from providers.factory import resolve_settings
from util.config import ConfigError, locate_output_dir

__all__ = ["describe_configuration", "router"]

router = APIRouter()


def describe_configuration() -> dict[str, Any]:
    """Summarise the current settings for the dashboard.

    Never includes the API key itself, only where it was (or was not)
    found. Missing settings are reported in ``problems`` instead of
    raising.
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
        "problems": check_configuration(),
    }
    try:
        settings = resolve_settings(require_key=False)
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


@router.get("/api/config")
async def config() -> dict[str, Any]:
    """Current provider, API key status and output folder (no secrets)."""
    return await run_in_threadpool(describe_configuration)


@router.get("/health")
async def health() -> dict[str, Any]:
    """Health check."""
    return {"status": "running", "version": VERSION}
