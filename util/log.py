"""Application logging: one namespace, configurable with ``LOG_LEVEL``.

Every module logs through :func:`get_logger`, so all messages share the
``paper_to_notebook`` prefix and one handler that prints them alongside
uvicorn's own output. Set ``LOG_LEVEL=DEBUG`` to also see previews of the
model's raw replies.
"""

from __future__ import annotations

import logging
import os
import sys

__all__ = [
    "APP_LOGGER",
    "LOG_LEVEL_ENV_VAR",
    "configure_logging",
    "get_logger",
    "mask_secret",
]

APP_LOGGER = "paper_to_notebook"
LOG_LEVEL_ENV_VAR = "LOG_LEVEL"
_FORMAT = "%(levelname)-9s %(asctime)s [%(name)s] %(message)s"
_DATE_FORMAT = "%H:%M:%S"
_PREFIX = f"{APP_LOGGER}."


def get_logger(module_name: str) -> logging.Logger:
    """Return the logger for a module, e.g. ``get_logger(__name__)``."""
    return logging.getLogger(f"{_PREFIX}{module_name}")


class _ShortNameFormatter(logging.Formatter):
    """Drop the shared ``paper_to_notebook.`` prefix from logger names."""

    def format(self, record: logging.LogRecord) -> str:
        record.name = record.name.removeprefix(_PREFIX)
        return super().format(record)


def configure_logging(level: str | None = None) -> None:
    """Send application logs to stderr (safe to call more than once).

    Args:
        level: Level name such as ``"INFO"``. Defaults to ``$LOG_LEVEL``,
            then ``INFO``.
    """
    level_name = (level or os.environ.get(LOG_LEVEL_ENV_VAR) or "INFO").upper()
    logger = logging.getLogger(APP_LOGGER)
    logger.setLevel(getattr(logging, level_name, logging.INFO))
    if not any(getattr(h, "_p2n", False) for h in logger.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(_ShortNameFormatter(_FORMAT, _DATE_FORMAT))
        handler._p2n = True  # type: ignore[attr-defined]
        logger.addHandler(handler)


def mask_secret(value: str | None) -> str:
    """Mask a secret for logging, keeping only enough to recognise it.

    Examples:
        >>> mask_secret("sk-ant-api03-abcdefghijklmnop7f2c")
        'sk-a…7f2c (33 chars)'
        >>> mask_secret("short")
        '**** (5 chars)'
        >>> mask_secret(None)
        '<not set>'
    """
    if not value:
        return "<not set>"
    if len(value) < 16:
        return f"**** ({len(value)} chars)"
    return f"{value[:4]}…{value[-4:]} ({len(value)} chars)"
