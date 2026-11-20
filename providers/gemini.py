"""Gemini provider, using the official Google Gen AI SDK."""

from __future__ import annotations

import json
import time
from typing import Any

from google import genai
from google.genai import errors, types

from providers.base import (
    MAX_ATTEMPTS,
    RETRY_INITIAL_DELAY,
    RETRY_MAX_DELAY,
    RETRYABLE_STATUS_CODES,
    LLMProvider,
    Progress,
    ProgressCallback,
    ProviderError,
    ProviderSettings,
    token_usage,
)
from util.log import get_logger
from util.url_utils import split_api_version

__all__ = ["GeminiProvider"]

logger = get_logger(__name__)


class GeminiProvider(LLMProvider):
    """Generate structured output with Gemini.

    Args:
        settings: Model, base URL and API key, usually from the factory.
        client: Optional preconfigured client (useful for tests).

    Raises:
        ProviderError: If no client is given and no API key was found.
    """

    def __init__(
        self,
        settings: ProviderSettings,
        *,
        client: genai.Client | None = None,
    ) -> None:
        super().__init__(settings)
        if client is None:
            if not settings.api_key:
                raise ProviderError(settings.missing_key_message())
            # The SDK does not retry unless asked to.
            http_options = types.HttpOptions(
                retry_options=types.HttpRetryOptions(
                    attempts=MAX_ATTEMPTS,
                    initial_delay=RETRY_INITIAL_DELAY,
                    max_delay=RETRY_MAX_DELAY,
                    http_status_codes=list(RETRYABLE_STATUS_CODES),
                )
            )
            if settings.base_url:
                root, version = split_api_version(settings.base_url)
                http_options.base_url = root
                http_options.api_version = version
            client = genai.Client(
                api_key=settings.api_key, http_options=http_options
            )
            logger.info(
                "Gemini client created: base_url=%s, api_version=%s, "
                "credentials=API key from %s, retries=up to %d attempts "
                "on HTTP %s",
                http_options.base_url or "<SDK default>",
                http_options.api_version or "<default>",
                settings.api_key_source,
                MAX_ATTEMPTS,
                ", ".join(map(str, RETRYABLE_STATUS_CODES)),
            )
        self._client = client

    def generate_json(
        self,
        prompt: str,
        *,
        schema: dict[str, Any],
        system: str | None = None,
        pdf: bytes | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        contents: list[Any] = []
        if pdf is not None:
            contents.append(
                types.Part.from_bytes(data=pdf, mime_type="application/pdf")
            )
        contents.append(prompt)

        config = types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_json_schema=schema,
        )
        logger.info(
            "Gemini request: model=%s, pdf=%s, prompt=%d chars, "
            "system=%d chars",
            self.model,
            f"{len(pdf) / 1024:.0f} KB" if pdf is not None else "none",
            len(prompt),
            len(system or ""),
        )
        started = time.monotonic()
        if on_progress is not None:
            # This request is not streamed, so there are no intermediate
            # updates between "waiting" and "done".
            on_progress(
                Progress("waiting", "Sent the paper to Gemini; waiting…")
            )
        try:
            response = self._client.models.generate_content(
                model=self.model, contents=contents, config=config
            )
        except errors.APIError as exc:
            retryable = exc.code in RETRYABLE_STATUS_CODES
            hint = (
                " A whole paper is a large request; free-tier Gemini quotas "
                "are often too small for it. Check your plan's rate limits "
                "and quota at ai.google.dev."
                if exc.code == 429
                else ""
            )
            raise ProviderError(
                f"Gemini API error {exc.code}: {exc.message}"
                + (
                    f" (still failing after {MAX_ATTEMPTS} attempts)"
                    if retryable
                    else ""
                )
                + hint,
                retryable=retryable,
            ) from exc

        candidates = response.candidates or []
        finish_reason = candidates[0].finish_reason if candidates else None
        usage = getattr(response, "usage_metadata", None)
        logger.info(
            "Gemini response in %.1fs: finish_reason=%s, input_tokens=%s, "
            "output_tokens=%s",
            time.monotonic() - started,
            finish_reason,
            getattr(usage, "prompt_token_count", "?"),
            getattr(usage, "candidates_token_count", "?"),
        )
        if finish_reason == types.FinishReason.MAX_TOKENS:
            raise ProviderError("Gemini's reply was cut off (MAX_TOKENS).")
        if not response.text:
            raise ProviderError(
                f"Gemini returned no content (finish reason: {finish_reason})."
            )
        logger.debug("Gemini reply (first 500 chars): %s", response.text[:500])
        if on_progress is not None:
            on_progress(
                Progress(
                    "done",
                    "Gemini finished.",
                    elapsed=time.monotonic() - started,
                    output_chars=len(response.text),
                    usage=_usage(usage),
                    served=_served(response, self.model),
                )
            )
        try:
            return json.loads(response.text)
        except json.JSONDecodeError as exc:
            raise ProviderError("Gemini returned invalid JSON.") from exc


def _usage(usage: Any) -> dict[str, int] | None:
    """Token counts from Gemini's usage metadata; thinking counts as output."""
    if usage is None:
        return None
    output = getattr(usage, "candidates_token_count", None)
    thoughts = getattr(usage, "thoughts_token_count", None)
    if isinstance(thoughts, int):
        output = (output if isinstance(output, int) else 0) + thoughts
    return token_usage(
        getattr(usage, "prompt_token_count", None),
        output,
        getattr(usage, "total_token_count", None),
    )


def _served(response: Any, requested_model: str) -> dict[str, Any]:
    """The model version Gemini reports; Gemini has no speed modes."""
    version = getattr(response, "model_version", None)
    return {
        "model": version
        if isinstance(version, str) and version
        else (requested_model),
        "speed": None,
        "model_fallback": False,
        "speed_fallback": False,
    }
