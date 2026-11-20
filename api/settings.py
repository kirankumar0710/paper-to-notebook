"""Constants shared by the API modules."""

from __future__ import annotations

from pathlib import Path

VERSION = "0.2.0"
# The PDF is sent inline (base64, ~33% larger), so keep it well under the
# providers' request size limits.
MAX_UPLOAD_MB = 15
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
PDF_MAGIC = b"%PDF-"
IPYNB_MEDIA_TYPE = "application/x-ipynb+json"
# Suggested wait before retrying when the model is temporarily unavailable.
RETRY_AFTER_SECONDS = 60
SAVED_PATH_HEADER = "X-Notebook-Path"

STATIC_DIR = Path(__file__).resolve().parent / "static"
UPLOAD_PAGE = STATIC_DIR / "index.html"
