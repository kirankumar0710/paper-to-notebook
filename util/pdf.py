"""Small PDF helpers."""

from __future__ import annotations

import io

from util.log import get_logger

__all__ = ["count_pages"]

logger = get_logger(__name__)


def count_pages(pdf: bytes) -> int | None:
    """Return the number of pages in ``pdf``, or None if it can't be read.

    Uses ``pypdf``. If it is not installed (a venv created before it was
    added to requirements.txt), the count is skipped rather than failing.
    """
    try:
        from pypdf import PdfReader
    except ImportError:
        logger.warning(
            "pypdf is not installed, so page counts are not shown. "
            "Run: venv/bin/pip install -r requirements.txt"
        )
        return None
    try:
        return len(PdfReader(io.BytesIO(pdf)).pages)
    except Exception as exc:  # noqa: BLE001 - any parse failure means "unknown"
        logger.info("Could not count PDF pages: %s", exc)
        return None
