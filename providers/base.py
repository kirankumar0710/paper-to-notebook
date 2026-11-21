"""Common interface and settings for LLM providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from util.log import mask_secret

__all__ = [
    "Progress",
    "ProgressCallback",
    "MAX_ATTEMPTS",
    "RETRYABLE_STATUS_CODES",
    "RETRY_INITIAL_DELAY",
    "RETRY_MAX_DELAY",
    "LLMProvider",
    "token_usage",
    "ProviderError",
    "ProviderSettings",
]


# Retries for temporary failures (overload, rate limits, network errors).
MAX_ATTEMPTS = 5  # including the first request
RETRY_INITIAL_DELAY = 2.0  # seconds; doubles after each attempt
RETRY_MAX_DELAY = 30.0  # seconds
RETRYABLE_STATUS_CODES = (408, 429, 500, 502, 503, 504)


class ProviderError(RuntimeError):
    """Raised when a provider cannot produce a response.

    Attributes:
        retryable: True when the failure is temporary (the service was
            overloaded or unreachable) and the same request may succeed
            if it is sent again later.
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class ProviderSettings:
    """Resolved settings for one provider.

    Attributes:
        name: Provider name, as used for ``PROVIDER`` and the config table.
        model: Model ID to call.
        base_url: API root, including the version segment (e.g. ``/v1``).
        api_key_env: Environment variables that may hold the key, in the
            order they are checked.
        api_key: The key found in the first set variable, if any.
        api_key_source: The variable the key was read from.
        options: Provider-specific tuning (e.g. ``effort``, ``speed``,
            ``max_tokens`` for Anthropic), from the config table or the
            ``PROVIDER_<OPTION>`` environment variables.
    """

    name: str
    model: str
    base_url: str | None = None
    api_key_env: tuple[str, ...] = ()
    api_key: str | None = None
    api_key_source: str | None = None
    options: dict[str, str] = field(default_factory=dict)

    def __repr__(self) -> str:
        # Never print the API key.
        return (
            f"ProviderSettings(name={self.name!r}, model={self.model!r}, "
            f"base_url={self.base_url!r}, api_key_env={self.api_key_env!r}, "
            f"api_key={mask_secret(self.api_key)!r}, options={self.options!r})"
        )

    def describe_key(self) -> str:
        """Say where the key came from, with the key itself masked."""
        if not self.api_key:
            return "not found (" + (", ".join(self.api_key_env) or "-") + ")"
        return f"found in {self.api_key_source}: {mask_secret(self.api_key)}"

    def missing_key_message(self) -> str:
        """Explain which environment variables to export."""
        names = " or ".join(self.api_key_env) or "an API key variable"
        return f"No API key for {self.name!r}. Export {names}."


@dataclass(frozen=True)
class Progress:
    """A status update while a conversion is running.

    Attributes:
        stage: ``waiting`` (request sent), ``thinking``, ``writing``,
            ``retrying``, ``building`` (assembling the notebook) or ``done``.
        message: Human-readable status line.
        elapsed: Seconds since the request was sent.
        output_chars: Characters of the reply received so far.
        thinking: Latest excerpt of the model's thinking summary, if any.
        thinking_delta: Thinking summary text received since the previous
            update, so a client can show the whole summary by appending.
        usage: Token counts (``input_tokens``, ``output_tokens``,
            ``total_tokens``), sent with the final ``done`` update when the
            provider reports them.
        served: What actually handled the request, sent with ``done``:
            ``model``, ``speed`` (``fast``/``standard``, or None if the
            provider has no speeds), ``model_fallback`` (a fallback model
            answered) and ``speed_fallback`` (fast mode fell back to
            standard).
    """

    stage: str
    message: str
    elapsed: float = 0.0
    output_chars: int = 0
    thinking: str | None = None
    thinking_delta: str | None = None
    usage: dict[str, int] | None = None
    served: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "message": self.message,
            "elapsed": round(self.elapsed, 1),
            "output_chars": self.output_chars,
            "thinking": self.thinking,
            "thinking_delta": self.thinking_delta,
            "usage": self.usage,
            "served": self.served,
        }


def token_usage(
    input_tokens: Any, output_tokens: Any, total_tokens: Any = None
) -> dict[str, int] | None:
    """Build a usage dict from SDK counts, or None if none are known.

    Counts that are not integers (missing from the response) become 0.
    The total defaults to input + output.
    """

    def count(value: Any) -> int | None:
        return value if isinstance(value, int) else None

    counts = [count(input_tokens), count(output_tokens), count(total_tokens)]
    if all(value is None for value in counts):
        return None
    inp, out, total = (value or 0 for value in counts)
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "total_tokens": total or inp + out,
    }


ProgressCallback = Callable[[Progress], None]


class LLMProvider(ABC):
    """A large language model that can read PDFs and return JSON."""

    def __init__(self, settings: ProviderSettings) -> None:
        self.settings = settings

    @property
    def name(self) -> str:
        return self.settings.name

    @property
    def model(self) -> str:
        return self.settings.model

    @property
    def requested_speed(self) -> str | None:
        """``fast`` or ``standard`` if the provider has speeds, else None."""
        return None

    @property
    def max_output_tokens(self) -> int | None:
        """The output token budget sent with each request, if any."""
        return None

    @abstractmethod
    def generate_json(
        self,
        prompt: str,
        *,
        schema: dict[str, Any],
        system: str | None = None,
        pdf: bytes | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        """Ask the model for a JSON object that matches ``schema``.

        Args:
            prompt: The user instruction.
            schema: JSON Schema the reply must follow.
            system: Optional system instructions.
            pdf: Optional PDF document for the model to read.
            on_progress: Called with :class:`Progress` updates while the
                request runs (from the calling thread).

        Returns:
            The parsed JSON object.

        Raises:
            ProviderError: If the request fails, the model declines, or the
                reply is cut off or is not valid JSON.
        """

    def __repr__(self) -> str:
        return f"{type(self).__name__}(model={self.model!r})"
