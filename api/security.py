"""HTTP hardening: allowed hosts, security headers and a cap on how many
conversions run at once.

- **Allowed hosts** stop DNS rebinding: a malicious page that points its
  own domain at 127.0.0.1 would otherwise be "same-origin" with this
  server, pass ``require_same_origin`` and, arriving from 127.0.0.1, the
  run check's localhost rule. Only loopback names are accepted, plus any
  listed in ``[app] allowed_hosts`` for a deploy.
- **Security headers** forbid framing (clickjacking), MIME sniffing and
  referrers, and set a Content-Security-Policy that allows only this
  server's own scripts, styles and requests.
- **Conversion slots** cap concurrent conversions, which spend money on
  the user's API key (``[app] max_concurrent_conversions``).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, status
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from util.config import Config, load_config
from util.log import get_logger

__all__ = [
    "LOOPBACK_HOSTS",
    "ConversionSlots",
    "HostCheckMiddleware",
    "SecurityHeadersMiddleware",
    "allowed_hosts",
    "host_allowed",
    "hostname",
    "is_loopback_host",
    "load_allowed_hosts",
    "load_max_conversions",
    "request_is_loopback",
]

logger = get_logger(__name__)

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
DEFAULT_MAX_CONVERSIONS = 2
RETRY_AFTER_BUSY = 30  # seconds

# The dashboard loads nothing from other origins. Swagger UI (/docs) and
# ReDoc load from a CDN, so they get only the framing rule.
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; "
    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
CSP_DOCS = "frame-ancestors 'none'"
DOCS_PATHS = ("/docs", "/redoc")


def hostname(host_header: str | None) -> str:
    """The host name in a ``Host`` header, without port or IPv6 brackets,
    lower-cased; "" if there is none."""
    if not host_header:
        return ""
    try:
        return (urlsplit(f"//{host_header}").hostname or "").lower()
    except ValueError:
        return ""


def is_loopback_host(host_header: str | None) -> bool:
    """Whether a ``Host`` header names this machine."""
    return hostname(host_header) in LOOPBACK_HOSTS


def host_allowed(host_header: str | None, allowed: frozenset[str]) -> bool:
    """Whether ``host_header`` matches an allowed name. ``*.example.com``
    matches subdomains; ``*`` allows every host."""
    name = hostname(host_header)
    if not name:
        return False
    if "*" in allowed or name in allowed:
        return True
    return any(
        pattern.startswith("*.") and name.endswith(pattern[1:])
        for pattern in allowed
    )


def load_allowed_hosts(config: Config | None = None) -> frozenset[str]:
    """Loopback names plus ``[app] allowed_hosts`` from the config."""
    if config is None:
        config = load_config()
    extra = config.get("app", {}).get("allowed_hosts", [])
    if not isinstance(extra, list) or not all(
        isinstance(host, str) for host in extra
    ):
        logger.warning("Ignoring invalid [app] allowed_hosts: %r", extra)
        extra = []
    return LOOPBACK_HOSTS | {h.strip().lower() for h in extra if h.strip()}


@lru_cache(maxsize=1)
def allowed_hosts() -> frozenset[str]:
    """The allowed hosts, read once from the config."""
    return load_allowed_hosts()


class HostCheckMiddleware:
    """Refuse requests whose ``Host`` is not an allowed name (400)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        host = _header(scope, b"host")
        if host_allowed(host, allowed_hosts()):
            await self.app(scope, receive, send)
            return
        logger.warning("Refused request for host %r", host)
        body = b"Invalid host header."
        await send(
            {
                "type": "http.response.start",
                "status": 400,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


class SecurityHeadersMiddleware:
    """Add security headers to every HTTP response."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        csp = CSP_DOCS if path.startswith(DOCS_PATHS) else CSP
        extra = [
            (b"content-security-policy", csp.encode()),
            (b"x-content-type-options", b"nosniff"),
            (b"x-frame-options", b"DENY"),
            (b"referrer-policy", b"no-referrer"),
        ]
        if scope.get("scheme") == "https":
            extra.append((b"strict-transport-security", b"max-age=31536000"))

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                names = {name.lower() for name, _ in message["headers"]}
                message["headers"] = [
                    *message["headers"],
                    *(item for item in extra if item[0] not in names),
                ]
            await send(message)

        await self.app(scope, receive, send_with_headers)


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key.lower() == name:
            return value.decode("latin-1")
    return None


def load_max_conversions(config: Config | None = None) -> int:
    """``[app] max_concurrent_conversions``, at least 1."""
    if config is None:
        config = load_config()
    value: Any = config.get("app", {}).get(
        "max_concurrent_conversions", DEFAULT_MAX_CONVERSIONS
    )
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        logger.warning(
            "Ignoring invalid [app] max_concurrent_conversions: %r", value
        )
        return DEFAULT_MAX_CONVERSIONS
    return value


class ConversionSlots:
    """How many conversions may run at once. Used from the event loop
    only, so a plain counter is enough."""

    def __init__(self, limit: int | None = None) -> None:
        self._limit = limit
        self.active = 0

    @property
    def limit(self) -> int:
        if self._limit is None:
            self._limit = load_max_conversions()
        return self._limit

    def acquire(self) -> None:
        """Take a slot, or raise 429 if all are in use."""
        if self.active >= self.limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    f"{self.active} conversion(s) are already running (the "
                    f"limit is {self.limit}). Try again when one finishes; "
                    "raise [app] max_concurrent_conversions to allow more."
                ),
                headers={"Retry-After": str(RETRY_AFTER_BUSY)},
            )
        self.active += 1

    def release(self) -> None:
        self.active = max(0, self.active - 1)


def request_is_loopback(request: Request) -> bool:
    """Whether the request names this machine in its ``Host`` header."""
    return is_loopback_host(request.headers.get("host"))
