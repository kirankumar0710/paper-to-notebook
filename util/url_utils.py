"""URL helpers used across the project."""

from __future__ import annotations

import re

__all__ = ["split_api_version"]

_VERSION_SEGMENT = re.compile(r"^v\d+(?:alpha|beta)?\d*$")


def split_api_version(url: str) -> tuple[str, str | None]:
    """Split a trailing API version segment off a base URL.

    Examples:
        >>> split_api_version("https://api.anthropic.com/v1")
        ('https://api.anthropic.com', 'v1')
        >>> split_api_version("https://example.com/api/")
        ('https://example.com/api', None)

    Returns:
        The URL without the version (and without a trailing slash), and the
        version segment, or ``None`` when there is none.
    """
    root, _, last = url.rstrip("/").rpartition("/")
    if root and _VERSION_SEGMENT.match(last):
        return root, last
    return url.rstrip("/"), None
