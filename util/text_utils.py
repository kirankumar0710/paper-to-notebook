"""String helpers used across the project."""

from __future__ import annotations

__all__ = ["sanitize_filename"]

_FILENAME_SAFE_PUNCTUATION = " -_."


def sanitize_filename(
    name: str,
    extension: str,
    *,
    max_length: int = 50,
    default: str = "untitled",
) -> str:
    """Build a filesystem- and header-safe filename from arbitrary text.

    Only ASCII letters, digits, spaces, hyphens, underscores and dots are
    kept, so the result can be used in an HTTP ``Content-Disposition``
    header. Leading and trailing dots and spaces are removed, so the result
    can never be a hidden file or a relative path like ``..``.

    Args:
        name: Free text to base the filename on (e.g. a paper title).
        extension: File extension including the dot, e.g. ``".ipynb"``.
        max_length: Maximum length of the stem before the extension.
        default: Stem to use if nothing safe remains.

    Returns:
        The sanitized filename.
    """
    stem = "".join(
        char
        for char in name
        if (char.isascii() and char.isalnum())
        or char in _FILENAME_SAFE_PUNCTUATION
    )
    stem = stem[:max_length].strip(" .")
    return f"{stem or default}{extension}"
