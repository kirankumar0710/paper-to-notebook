"""Filesystem helpers used across the project."""

from __future__ import annotations

from pathlib import Path

__all__ = ["save_unique"]


def save_unique(directory: Path, filename: str, text: str) -> Path:
    """Write ``text`` to ``directory/filename`` without overwriting.

    If the name is taken, `` (1)``, `` (2)``… is added before the extension,
    as browsers do for downloads.

    Args:
        directory: Folder to write into; created if missing.
        filename: Desired file name, e.g. ``"paper.ipynb"``.
        text: File contents (written as UTF-8).

    Returns:
        The path the file was written to.
    """
    directory.mkdir(parents=True, exist_ok=True)
    stem, suffix = Path(filename).stem, Path(filename).suffix
    path = directory / filename
    counter = 1
    while True:
        try:
            # "x" mode fails if the file exists, so two requests can never
            # write to the same path.
            with path.open("x", encoding="utf-8") as handle:
                handle.write(text)
            return path
        except FileExistsError:
            path = directory / f"{stem} ({counter}){suffix}"
            counter += 1
