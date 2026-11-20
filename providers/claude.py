"""Anthropic (Claude) provider, using the official Anthropic SDK."""

from __future__ import annotations

import base64
import json
import time
from typing import Any

import anthropic

from providers.base import (
    MAX_ATTEMPTS,
    RETRYABLE_STATUS_CODES,
    LLMProvider,
    Progress,
    ProgressCallback,
    ProviderError,
    ProviderSettings,
)
from util.log import get_logger
from util.url_utils import split_api_version

__all__ = ["ClaudeProvider"]

FALLBACK_BETA = "server-side-fallback-2026-07-01"

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
SPEEDS = ("standard", "fast")
DEFAULT_EFFORT = "medium"
DEFAULT_SPEED = "standard"
# Thinking and the notebook share this budget; 128K is the model's maximum
# and needs streaming, which this provider always uses.
DEFAULT_MAX_TOKENS = 128000

# How often to log progress while a reply streams in, in seconds.
PROGRESS_LOG_INTERVAL = 15.0
# How often to send progress to an on_progress callback, in seconds.
PROGRESS_EMIT_INTERVAL = 1.0
# Characters of the thinking summary included in each progress update.
THINKING_EXCERPT_CHARS = 400

logger = get_logger(__name__)


class _StreamTracker:
    """Turn streaming events into log lines and progress callbacks."""

    def __init__(
        self, started: float, on_progress: ProgressCallback | None
    ) -> None:
        self.started = started
        self.on_progress = on_progress
        self.stage = "waiting"
        self.text_chars = 0
        self.thinking_text = ""
        self.last_log = started
        self.last_emit = 0.0

    def emit(self, stage: str, message: str, *, force: bool = False) -> None:
        now = time.monotonic()
        if now - self.last_log >= PROGRESS_LOG_INTERVAL:
            self.last_log = now
            logger.info(
                "Anthropic still working: %.0fs elapsed, %s, "
                "%d chars of output so far",
                now - self.started,
                stage,
                self.text_chars,
            )
        if self.on_progress is None:
            return
        if not force and now - self.last_emit < PROGRESS_EMIT_INTERVAL:
            return
        self.last_emit = now
        excerpt = self.thinking_text[-THINKING_EXCERPT_CHARS:].strip()
        self.on_progress(
            Progress(
                stage=stage,
                message=message,
                elapsed=now - self.started,
                output_chars=self.text_chars,
                thinking=excerpt or None,
            )
        )

    def handle(self, event: Any) -> None:
        if event.type == "content_block_start":
            block_type = getattr(event.content_block, "type", "")
            if block_type == "thinking":
                self.stage = "thinking"
                self.emit("thinking", "Claude is thinking…", force=True)
            elif block_type == "text":
                self.stage = "writing"
                self.emit(
                    "writing", "Claude is writing the notebook…", force=True
                )
            return
        if event.type == "content_block_delta":
            delta_type = getattr(event.delta, "type", "")
            if delta_type == "thinking_delta":
                self.thinking_text += getattr(event.delta, "thinking", "")
            elif delta_type == "text_delta":
                self.text_chars += len(event.delta.text)
        if self.stage == "writing":
            message = f"Writing the notebook ({self.text_chars:,} characters)"
        else:
            message = "Claude is thinking…"
        self.emit(self.stage, message)


class ClaudeProvider(LLMProvider):
    """Generate structured output with Claude.

    Tuning comes from the keyword arguments, else from ``settings.options``
    (``effort``, ``speed`` and ``max_tokens`` in ``config.ini`` or the
    ``PROVIDER_EFFORT`` / ``PROVIDER_SPEED`` / ``PROVIDER_MAX_TOKENS``
    environment variables), else from the defaults.

    Args:
        settings: Model, base URL, API key and options from the factory.
        effort: ``low``, ``medium``, ``high``, ``xhigh`` or ``max``. Higher
            effort thinks longer: slower, and it uses more of max_tokens.
        speed: ``standard`` or ``fast``. Fast mode generates up to ~2.5x
            faster at twice the per-token price; if it is rate limited, the
            request falls back to standard speed.
        max_tokens: Output budget shared by thinking and the reply.
        fallbacks: ``"default"`` lets the API retry a declined request on a
            fallback model; an empty string turns this off.
        client: Optional preconfigured client (useful for tests).

    Raises:
        ProviderError: If an option is invalid, or no client is given and
            no API key was found.
    """

    def __init__(
        self,
        settings: ProviderSettings,
        *,
        effort: str | None = None,
        speed: str | None = None,
        max_tokens: int | None = None,
        fallbacks: str = "default",
        client: anthropic.Anthropic | None = None,
    ) -> None:
        super().__init__(settings)
        options = settings.options
        self.effort = (
            effort or options.get("effort") or DEFAULT_EFFORT
        ).lower()
        self.speed = (speed or options.get("speed") or DEFAULT_SPEED).lower()
        self.max_tokens = _parse_max_tokens(
            max_tokens or options.get("max_tokens") or DEFAULT_MAX_TOKENS
        )
        self.fallbacks = fallbacks
        if self.effort not in EFFORT_LEVELS:
            raise ProviderError(
                f"Invalid effort {self.effort!r}; use one of "
                f"{', '.join(EFFORT_LEVELS)} (PROVIDER_EFFORT)."
            )
        if self.speed not in SPEEDS:
            raise ProviderError(
                f"Invalid speed {self.speed!r}; use one of "
                f"{', '.join(SPEEDS)} (PROVIDER_SPEED)."
            )

        if client is None:
            if not settings.api_key:
                raise ProviderError(settings.missing_key_message())
            # The SDK retries 408/409/429/5xx and connection errors itself.
            client_options: dict[str, Any] = {"max_retries": MAX_ATTEMPTS - 1}
            if settings.base_url:
                # The SDK adds the "/v1" segment itself.
                root, _ = split_api_version(settings.base_url)
                client_options["base_url"] = root
            client_options["api_key"] = settings.api_key
            client = anthropic.Anthropic(**client_options)
            logger.info(
                "Anthropic client created: base_url=%s, credentials=API key "
                "from %s, retries=up to %d attempts",
                client.base_url,
                settings.api_key_source,
                MAX_ATTEMPTS,
            )
        self._client = client

    def _build_request(
        self,
        prompt: str,
        schema: dict[str, Any],
        system: str | None,
        pdf: bytes | None,
    ) -> dict[str, Any]:
        content: list[dict[str, Any]] = []
        if pdf is not None:
            content.append(
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": base64.standard_b64encode(pdf).decode(),
                    },
                }
            )
        content.append({"type": "text", "text": prompt})

        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            # "summarized" streams a readable summary of the reasoning, which
            # is shown as progress; by default thinking text is empty.
            "thinking": {"type": "adaptive", "display": "summarized"},
            "output_config": {
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": schema},
            },
            "messages": [{"role": "user", "content": content}],
        }
        if system:
            request["system"] = system
        if self.fallbacks:
            request["betas"] = [FALLBACK_BETA]
            request["fallbacks"] = self.fallbacks
        if self.speed == "fast":
            request["speed"] = "fast"
        return request

    def _stream(
        self,
        client: anthropic.Anthropic,
        request: dict[str, Any],
        tracker: _StreamTracker,
    ) -> Any:
        with client.beta.messages.stream(**request) as stream:
            for event in stream:
                tracker.handle(event)
            return stream.get_final_message()

    def _send(self, request: dict[str, Any], tracker: _StreamTracker) -> Any:
        """Send the request; fall back to standard speed if fast fails."""
        if request.get("speed") != "fast":
            return self._stream(self._client, request, tracker)
        try:
            # Fail fast on fast mode's own rate limit, then use standard.
            return self._stream(
                self._client.with_options(max_retries=0), request, tracker
            )
        except anthropic.RateLimitError as exc:
            reason = f"fast mode rate limited ({exc.status_code})"
        except anthropic.BadRequestError as exc:
            if "speed" not in str(exc.message).lower():
                raise
            reason = f"fast mode not available: {exc.message}"
        logger.warning("Retrying at standard speed: %s", reason)
        tracker.emit(
            "retrying",
            "Fast mode is busy; retrying at standard speed…",
            force=True,
        )
        standard = {k: v for k, v in request.items() if k != "speed"}
        return self._stream(self._client, standard, tracker)

    def generate_json(
        self,
        prompt: str,
        *,
        schema: dict[str, Any],
        system: str | None = None,
        pdf: bytes | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        request = self._build_request(prompt, schema, system, pdf)
        logger.info(
            "Anthropic request: model=%s, effort=%s, speed=%s, "
            "max_tokens=%d, fallbacks=%s, pdf=%s, prompt=%d chars, "
            "system=%d chars",
            self.model,
            self.effort,
            self.speed,
            self.max_tokens,
            self.fallbacks or "off",
            f"{len(pdf) / 1024:.0f} KB" if pdf is not None else "none",
            len(prompt),
            len(system or ""),
        )
        started = time.monotonic()
        tracker = _StreamTracker(started, on_progress)
        tracker.emit(
            "waiting", "Sent the paper to Claude; waiting…", force=True
        )
        try:
            response = self._send(request, tracker)
        except anthropic.AuthenticationError as exc:
            raise ProviderError(
                "Anthropic rejected the API key. "
                f"{self.settings.missing_key_message()}"
            ) from exc
        except anthropic.RateLimitError as exc:
            raise ProviderError(
                "Anthropic rate limit reached.", retryable=True
            ) from exc
        except anthropic.APIStatusError as exc:
            # 529 is Anthropic's "overloaded" status.
            retryable = exc.status_code in (*RETRYABLE_STATUS_CODES, 529)
            raise ProviderError(
                f"Anthropic API error {exc.status_code}: {exc.message}",
                retryable=retryable,
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError(
                "Could not connect to the Anthropic API.", retryable=True
            ) from exc

        usage = getattr(response, "usage", None)
        logger.info(
            "Anthropic response in %.1fs: stop_reason=%s, served_by=%s, "
            "speed=%s, input_tokens=%s, output_tokens=%s",
            time.monotonic() - started,
            response.stop_reason,
            getattr(response, "model", self.model),
            getattr(usage, "speed", None) or self.speed,
            getattr(usage, "input_tokens", "?"),
            getattr(usage, "output_tokens", "?"),
        )

        if response.stop_reason == "refusal":
            raise ProviderError("Claude declined to answer this request.")
        if response.stop_reason == "max_tokens":
            raise ProviderError(
                f"Claude's reply was cut off at max_tokens={self.max_tokens} "
                f"(thinking and the notebook share this budget). Lower "
                f"PROVIDER_EFFORT (now {self.effort}) or raise "
                f"PROVIDER_MAX_TOKENS."
            )

        text = "".join(
            block.text for block in response.content if block.type == "text"
        )
        logger.debug("Anthropic reply (first 500 chars): %s", text[:500])
        tracker.emit("done", "Claude finished.", force=True)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProviderError("Claude returned invalid JSON.") from exc


def _parse_max_tokens(value: int | str) -> int:
    """Validate the max_tokens option."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = 0
    if number <= 0:
        raise ProviderError(
            f"Invalid max_tokens {value!r}; use a positive integer "
            "(PROVIDER_MAX_TOKENS)."
        )
    return number
