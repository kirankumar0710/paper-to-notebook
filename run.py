#!/usr/bin/env python3
"""Entry point for the Paper-to-Notebook server.

On first run this creates a local virtual environment, installs the
dependencies into it, and re-executes itself with the venv's interpreter.
"""

from __future__ import annotations

import os
import subprocess
import sys
import venv
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
VENV_DIR = PROJECT_DIR / "venv"
VENV_PYTHON = VENV_DIR / (
    "Scripts/python.exe" if os.name == "nt" else "bin/python3"
)
REQUIREMENTS = PROJECT_DIR / "requirements.txt"

HOST = "0.0.0.0"
PORT = 8000


def setup_venv() -> None:
    """Create the venv and install dependencies if it doesn't exist yet."""
    if VENV_PYTHON.exists():
        return

    print("🔧 First run — setting up virtual environment...")
    venv.create(VENV_DIR, with_pip=True)
    print("📦 Installing dependencies (this takes ~30 seconds)...")
    subprocess.check_call(
        [
            str(VENV_PYTHON),
            "-m",
            "pip",
            "install",
            "-r",
            str(REQUIREMENTS),
            "-q",
        ],
    )
    print("✅ Setup complete!\n")


def report_provider() -> bool:
    """Print the provider, API key and output folder, or what is missing.

    Returns:
        True if everything required is configured.
    """
    from providers.factory import resolve_settings
    from util.config import resolve_output_dir

    ok = True
    try:
        settings = resolve_settings()
    except (ValueError, FileNotFoundError) as exc:
        print(f"⚠️  LLM provider not configured: {exc}")
        ok = False
    else:
        print(f"🤖 LLM provider: {settings.name} ({settings.model})")
        print(f"🔑 API key {settings.describe_key()}")

    try:
        print(f"📁 Output folder: {resolve_output_dir()}")
    except ValueError as exc:
        print(f"⚠️  {exc}")
        ok = False

    if not ok:
        print("⚠️  /convert will fail until the variables above are exported.")
    return ok


def main() -> None:
    """Ensure we're running inside the venv, then start the server."""
    setup_venv()

    if Path(sys.executable) != VENV_PYTHON:
        os.execv(VENV_PYTHON, [str(VENV_PYTHON), __file__])

    report_provider()

    import uvicorn

    uvicorn.run("api.main:app", host=HOST, port=PORT, reload=True)


if __name__ == "__main__":
    main()
