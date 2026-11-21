"""Create the LLM provider selected by the ``PROVIDER`` environment variable.

Each setting is resolved from the first source that has it:

=========  =====================  =====================  =================
Setting    get_provider() kwarg   Environment variable   TOML files
=========  =====================  =====================  =================
provider   ``name``               ``PROVIDER``           —  (required)
model      ``model``              ``PROVIDER_MODEL``     ``model``
base URL   ``base_url``           ``PROVIDER_BASE_URL``  ``base_url``
API key    —                      listed in ``api_key_env``  never (required)
=========  =====================  =====================  =================

TOML files are ``config.toml`` overlaid with ``$PAPER_TO_NOTEBOOK_CONFIG``.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from providers.base import LLMProvider, ProviderSettings
from util.config import (
    Config,
    ConfigError,
    load_config,
    locate_api_key,
    parse_env_var_list,
)
from util.log import get_logger

__all__ = [
    "BASE_URL_ENV_VAR",
    "MODEL_ENV_VAR",
    "PROVIDER_ENV_VAR",
    "SUPPORTED_PROVIDERS",
    "get_provider",
    "resolve_provider_name",
    "resolve_settings",
]

PROVIDER_ENV_VAR = "PROVIDER"
MODEL_ENV_VAR = "PROVIDER_MODEL"
BASE_URL_ENV_VAR = "PROVIDER_BASE_URL"

SUPPORTED_PROVIDERS = ("anthropic", "gemini", "openai")

# Alternative spellings accepted for PROVIDER.
_ALIASES = {
    "claude": "anthropic",
    "google": "gemini",
    "gpt": "openai",
    "chatgpt": "openai",
}

# Tuning options each provider accepts. Each is read from the provider's
# config table and can be overridden with PROVIDER_<OPTION>, e.g.
# PROVIDER_EFFORT.
PROVIDER_OPTIONS: dict[str, tuple[str, ...]] = {
    "anthropic": ("effort", "speed", "max_tokens"),
    "gemini": (),
    "openai": ("effort", "max_tokens"),
}

logger = get_logger(__name__)


def _anthropic(settings: ProviderSettings) -> LLMProvider:
    from providers.claude import ClaudeProvider

    return ClaudeProvider(settings)


def _gemini(settings: ProviderSettings) -> LLMProvider:
    from providers.gemini import GeminiProvider

    return GeminiProvider(settings)


def _openai(settings: ProviderSettings) -> LLMProvider:
    from providers.openai_provider import OpenAIProvider

    return OpenAIProvider(settings)


_BUILDERS: dict[str, Callable[[ProviderSettings], LLMProvider]] = {
    "anthropic": _anthropic,
    "gemini": _gemini,
    "openai": _openai,
}


def _first_set(*candidates: tuple[Any, str]) -> tuple[str, str]:
    """Return ``(value, source)`` for the first non-empty candidate.

    Config values may be numbers (``max_tokens = 128000``); they are
    returned as text, like environment variables.
    """
    for value, source in candidates:
        text = "" if value is None else str(value).strip()
        if text:
            return text, source
    return "", "not set"


def resolve_provider_name(name: str | None = None) -> str:
    """Return the canonical provider name from ``name`` or ``PROVIDER``.

    Raises:
        ConfigError: If ``PROVIDER`` is not set (and no ``name`` is given)
            or names an unsupported provider.
    """
    supported = ", ".join(SUPPORTED_PROVIDERS)
    if name:
        raw, source = name, "argument"
    elif os.environ.get(PROVIDER_ENV_VAR, "").strip():
        raw, source = os.environ[PROVIDER_ENV_VAR], f"${PROVIDER_ENV_VAR}"
    else:
        raise ConfigError(
            f"No LLM provider selected: export {PROVIDER_ENV_VAR} "
            f"(one of: {supported})."
        )
    provider_name = raw.strip().lower()
    provider_name = _ALIASES.get(provider_name, provider_name)
    if provider_name not in _BUILDERS:
        raise ConfigError(
            f"Unknown provider {raw!r}. "
            f"Set {PROVIDER_ENV_VAR} to one of: {supported}."
        )
    logger.info("Provider: %s (from %s)", provider_name, source)
    return provider_name


def resolve_settings(
    name: str | None = None,
    *,
    model: str | None = None,
    base_url: str | None = None,
    config: Config | None = None,
    require_key: bool = True,
    require_model: bool = True,
) -> ProviderSettings:
    """Resolve the provider's settings without creating an SDK client.

    Args:
        require_key: When False, a missing API key is not an error; the
            returned settings have ``api_key=None`` (used to show the
            configuration).
        require_model: When False, a missing model is not an error; the
            returned settings have ``model=""``.

    Raises:
        ConfigError: If the provider is missing or unknown, no model is
            configured (when ``require_model``), or (when ``require_key``)
            none of the provider's API key variables is set.
    """
    provider_name = resolve_provider_name(name)
    if config is None:
        config = load_config()
    section = config.get(provider_name, {})

    resolved_model, model_source = _first_set(
        (model, "argument"),
        (os.environ.get(MODEL_ENV_VAR), f"${MODEL_ENV_VAR}"),
        (section.get("model"), f"config [{provider_name}] model"),
    )
    if not resolved_model and require_model:
        raise ConfigError(
            f"No model configured for {provider_name!r}. Set "
            f"{MODEL_ENV_VAR} or 'model' under [{provider_name}]."
        )

    resolved_base_url, base_url_source = _first_set(
        (base_url, "argument"),
        (os.environ.get(BASE_URL_ENV_VAR), f"${BASE_URL_ENV_VAR}"),
        (section.get("base_url"), f"config [{provider_name}] base_url"),
    )

    options = {}
    for option in PROVIDER_OPTIONS[provider_name]:
        env_var = f"PROVIDER_{option.upper()}"
        value, source = _first_set(
            (os.environ.get(env_var), f"${env_var}"),
            (section.get(option), f"config [{provider_name}] {option}"),
        )
        if value:
            options[option] = value
            logger.info("Option %s: %s (from %s)", option, value, source)

    api_key_env = parse_env_var_list(section.get("api_key_env"))
    found = locate_api_key(api_key_env)
    settings = ProviderSettings(
        name=provider_name,
        model=resolved_model,
        base_url=resolved_base_url.rstrip("/") or None,
        api_key_env=api_key_env,
        api_key=found[1] if found else None,
        api_key_source=found[0] if found else None,
        options=options,
    )

    logger.info("Model: %s (from %s)", settings.model, model_source)
    logger.info(
        "Base URL: %s (from %s)",
        settings.base_url or "<SDK default>",
        base_url_source,
    )
    if not settings.api_key:
        if require_key:
            raise ConfigError(settings.missing_key_message())
        return settings
    logger.info("API key: %s", settings.describe_key())
    return settings


def get_provider(
    name: str | None = None,
    *,
    model: str | None = None,
    base_url: str | None = None,
    config: Config | None = None,
) -> LLMProvider:
    """Build the LLM provider selected by ``PROVIDER`` (or ``name``).

    Args:
        name: ``"anthropic"``, ``"gemini"`` or ``"openai"``; overrides
            ``PROVIDER``.
        model: Overrides ``PROVIDER_MODEL`` and the config ``model``.
        base_url: Overrides ``PROVIDER_BASE_URL`` and the config ``base_url``.
        config: Parsed config. Defaults to :func:`util.config.load_config`.

    Returns:
        A ready-to-use provider.

    Raises:
        ConfigError: If the provider is missing or unknown, no model is
            configured, or none of the provider's API key variables is set.
    """
    settings = resolve_settings(
        name, model=model, base_url=base_url, config=config
    )
    provider = _BUILDERS[settings.name](settings)
    logger.info("Provider ready: %r", provider)
    return provider
