"""OpenAI provider, using the official OpenAI SDK (Responses API)."""

from __future__ import annotations

import base64
import json
import time
from typing import Any

import openai

from providers.base import (
    MAX_ATTEMPTS,
    RETRYABLE_STATUS_CODES,
    LLMProvider,
    Progress,
    ProgressCallback,
    ProviderError,
    ProviderSettings,
    token_usage,
)
from util.log import get_logger

__all__ = ["OpenAIProvider"]

# Reasoning effort values the Responses API accepts.
EFFORT_LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
# The reply is not streamed, and reading a whole paper can take several
# minutes on a reasoning model, so allow longer than the SDK's 10 minutes.
REQUEST_TIMEOUT_SECONDS = 30 * 60
SCHEMA_NAME = "colab_notebook"
PDF_FILENAME = "paper.pdf"

logger = get_logger(__name__)


class OpenAIProvider(LLMProvider):
    """Generate structured output with an OpenAI model.

    Tuning comes from ``settings.options`` (``effort`` and ``max_tokens``
    under ``[openai]`` in ``config.toml``, or ``PROVIDER_EFFORT`` /
    ``PROVIDER_MAX_TOKENS``). Neither is sent unless set, so models without
    reasoning still work.

    Args:
        settings: Model, base URL, API key and options from the factory.
        client: Optional preconfigured client (useful for tests).

    Raises:
        ProviderError: If an option is invalid, or no client is given and
            no API key was found.
    """

    def __init__(
        self,
        settings: ProviderSettings,
        *,
        client: openai.OpenAI | None = None,
    ) -> None:
        super().__init__(settings)
        options = settings.options
        effort = options.get("effort")
        self.effort = effort.lower() if effort else None
        if self.effort is not None and self.effort not in EFFORT_LEVELS:
            raise ProviderError(
                f"Invalid effort {self.effort!r}; use one of "
                f"{', '.join(EFFORT_LEVELS)} (PROVIDER_EFFORT)."
            )
        self.max_tokens = _parse_max_tokens(options.get("max_tokens"))

        if client is None:
            if not settings.api_key:
                raise ProviderError(settings.missing_key_message())
            # The SDK retries 408/409/429/5xx and connection errors itself.
            client_options: dict[str, Any] = {
                "api_key": settings.api_key,
                "max_retries": MAX_ATTEMPTS - 1,
                "timeout": REQUEST_TIMEOUT_SECONDS,
            }
            if settings.base_url:
                # The OpenAI SDK expects the version segment ("/v1").
                client_options["base_url"] = settings.base_url
            client = openai.OpenAI(**client_options)
            logger.info(
                "OpenAI client created: base_url=%s, credentials=API key "
                "from %s, retries=up to %d attempts",
                client.base_url,
                settings.api_key_source,
                MAX_ATTEMPTS,
            )
        self._client = client

    @property
    def max_output_tokens(self) -> int | None:
        return self.max_tokens

    def _build_request(
        self,
        prompt: str,
        schema: dict[str, Any],
        system: str | None,
        pdf: bytes | None,
    ) -> dict[str, Any]:
        content: list[dict[str, Any]] = []
        if pdf is not None:
            encoded = base64.standard_b64encode(pdf).decode()
            content.append(
                {
                    "type": "input_file",
                    "filename": PDF_FILENAME,
                    "file_data": f"data:application/pdf;base64,{encoded}",
                }
            )
        content.append({"type": "input_text", "text": prompt})
        request: dict[str, Any] = {
            "model": self.model,
            "input": [{"role": "user", "content": content}],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": SCHEMA_NAME,
                    "schema": schema,
                    "strict": True,
                }
            },
            # Don't keep the paper and notebook on OpenAI's side.
            "store": False,
        }
        if system:
            request["instructions"] = system
        if self.effort:
            request["reasoning"] = {"effort": self.effort}
        if self.max_tokens:
            request["max_output_tokens"] = self.max_tokens
        return request

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
            "OpenAI request: model=%s, effort=%s, max_output_tokens=%s, "
            "pdf=%s, prompt=%d chars, system=%d chars",
            self.model,
            self.effort or "<model default>",
            self.max_tokens or "<model default>",
            f"{len(pdf) / 1024:.0f} KB" if pdf is not None else "none",
            len(prompt),
            len(system or ""),
        )
        started = time.monotonic()
        if on_progress is not None:
            # This request is not streamed, so there are no intermediate
            # updates between "waiting" and "done".
            on_progress(
                Progress("waiting", "Sent the paper to OpenAI; waiting…")
            )
        try:
            response = self._client.responses.create(**request)
        except openai.AuthenticationError as exc:
            raise ProviderError(
                "OpenAI rejected the API key. "
                f"{self.settings.missing_key_message()}"
            ) from exc
        except openai.RateLimitError as exc:
            raise _rate_limit_error(exc, self.model) from exc
        except openai.APIStatusError as exc:
            raise ProviderError(
                f"OpenAI API error {exc.status_code}: {exc.message}",
                retryable=exc.status_code in RETRYABLE_STATUS_CODES,
            ) from exc
        except openai.APIConnectionError as exc:
            # Also covers timeouts (APITimeoutError is a subclass).
            raise ProviderError(
                "Could not connect to the OpenAI API.", retryable=True
            ) from exc

        usage = getattr(response, "usage", None)
        logger.info(
            "OpenAI response in %.1fs: status=%s, served_by=%s, "
            "input_tokens=%s, output_tokens=%s",
            time.monotonic() - started,
            getattr(response, "status", None),
            getattr(response, "model", self.model),
            getattr(usage, "input_tokens", "?"),
            getattr(usage, "output_tokens", "?"),
        )
        _check_complete(response, self.max_tokens)

        text = getattr(response, "output_text", "") or ""
        if not text:
            raise ProviderError("OpenAI returned no content.")
        logger.debug("OpenAI reply (first 500 chars): %s", text[:500])
        if on_progress is not None:
            on_progress(
                Progress(
                    "done",
                    "OpenAI finished.",
                    elapsed=time.monotonic() - started,
                    output_chars=len(text),
                    usage=token_usage(
                        getattr(usage, "input_tokens", None),
                        getattr(usage, "output_tokens", None),
                        getattr(usage, "total_tokens", None),
                    ),
                    served=_served(response, self.model),
                )
            )
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProviderError("OpenAI returned invalid JSON.") from exc


def _rate_limit_error(exc: openai.RateLimitError, model: str) -> ProviderError:
    """Explain a 429, and only mark it retryable when waiting can help."""
    reason = exc.message or ""
    if getattr(exc, "code", None) == "insufficient_quota":
        return ProviderError(
            "OpenAI says the account has no quota left. Check your plan and "
            f"billing at platform.openai.com. ({reason})"
        )
    if "request too large" in reason.lower():
        # The whole paper is more tokens than the account may send in a
        # minute, so retrying the same request can never succeed.
        return ProviderError(
            f"This paper needs more tokens than your OpenAI account's "
            f"per-minute limit for {model} allows in one request. Use a "
            f"higher usage tier, a model with a higher limit "
            f"(PROVIDER_MODEL), or a shorter PDF. ({reason})"
        )
    return ProviderError(
        f"OpenAI rate limit reached: {reason}", retryable=True
    )


def _check_complete(response: Any, max_tokens: int | None) -> None:
    """Raise if the reply was cut off or the model declined."""
    if getattr(response, "status", None) == "incomplete":
        details = getattr(response, "incomplete_details", None)
        reason = getattr(details, "reason", None)
        if reason == "max_output_tokens":
            budget = f" ({max_tokens})" if max_tokens else ""
            raise ProviderError(
                f"OpenAI's reply was cut off at max_output_tokens{budget} "
                "(reasoning and the notebook share this budget). Lower "
                "PROVIDER_EFFORT or raise PROVIDER_MAX_TOKENS."
            )
        if reason == "content_filter":
            raise ProviderError("OpenAI declined to answer this request.")
        raise ProviderError(f"OpenAI's reply was incomplete ({reason}).")
    for item in getattr(response, "output", None) or []:
        if getattr(item, "type", None) != "message":
            continue
        for part in getattr(item, "content", None) or []:
            if getattr(part, "type", None) == "refusal":
                raise ProviderError(
                    "OpenAI declined to answer this request: "
                    f"{getattr(part, 'refusal', '')}".rstrip(": ")
                )


def _served(response: Any, requested_model: str) -> dict[str, Any]:
    """The model OpenAI reports; it has no fast/standard speed modes."""
    model = getattr(response, "model", None)
    return {
        "model": model
        if isinstance(model, str) and model
        else (requested_model),
        "speed": None,
        "model_fallback": False,
        "speed_fallback": False,
    }


def _parse_max_tokens(value: str | int | None) -> int | None:
    """Validate the optional max_tokens option."""
    if value in (None, ""):
        return None
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
