"""Estimate what a conversion cost from its token counts.

Prices come from the config (see config.toml): ``[pricing.models]`` maps
each model id to ``{ input = <USD>, output = <USD> }`` per million tokens at
standard speed, and ``[pricing] fast_multiplier`` applies to fast mode.
"""

from __future__ import annotations

from typing import Any

from util.config import Config, load_config
from util.log import get_logger

__all__ = ["estimate_cost", "load_prices"]

SECTION = "pricing"
MODELS_KEY = "models"
FAST_MULTIPLIER_KEY = "fast_multiplier"
TOKENS_PER_PRICE_UNIT = 1_000_000

logger = get_logger(__name__)


def load_prices(
    config: Config | None = None,
) -> tuple[dict[str, tuple[float, float]], float]:
    """Return ``({model: (input, output)}, fast_multiplier)``.

    Entries that cannot be parsed are skipped with a warning.
    """
    if config is None:
        config = load_config()
    section = config.get(SECTION, {})
    multiplier = _number(section.get(FAST_MULTIPLIER_KEY, 1))
    if multiplier is None:
        logger.warning("Ignoring invalid [pricing] fast_multiplier")
        multiplier = 1.0
    models = section.get(MODELS_KEY, {})
    if not isinstance(models, dict):
        logger.warning("Ignoring [pricing] models: it must be a table")
        models = {}
    prices: dict[str, tuple[float, float]] = {}
    for model, value in models.items():
        rates = value if isinstance(value, dict) else {}
        input_price = _number(rates.get("input"))
        output_price = _number(rates.get("output"))
        if input_price is None or output_price is None:
            logger.warning(
                "Ignoring invalid [pricing.models] entry for %s; use "
                "{ input = <USD>, output = <USD> }",
                model,
            )
            continue
        prices[model.lower()] = (input_price, output_price)
    return prices, multiplier


def _number(value: Any) -> float | None:
    """A price or multiplier as a float, or None if it isn't a number."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def estimate_cost(
    model: str,
    usage: dict[str, int] | None,
    *,
    speed: str | None = None,
    config: Config | None = None,
) -> dict[str, Any] | None:
    """Estimate the cost of one request in USD.

    Args:
        model: The model that answered.
        usage: ``input_tokens`` and ``output_tokens``.
        speed: ``fast`` applies the fast-mode multiplier.
        config: Parsed config. Defaults to :func:`util.config.load_config`.

    Returns:
        ``input_usd``, ``output_usd``, ``total_usd``, the per-million rates
        used and the model, or None if there is no usage or no price for
        the model.
    """
    if not usage:
        return None
    prices, multiplier = load_prices(config)
    rates = prices.get(model.lower())
    if rates is None:
        return None
    factor = multiplier if speed == "fast" else 1.0
    input_rate, output_rate = (rate * factor for rate in rates)
    input_usd = (
        usage.get("input_tokens", 0) * input_rate / TOKENS_PER_PRICE_UNIT
    )
    output_usd = (
        usage.get("output_tokens", 0) * output_rate / TOKENS_PER_PRICE_UNIT
    )
    return {
        "model": model,
        "speed": speed,
        "input_rate": input_rate,
        "output_rate": output_rate,
        "input_usd": round(input_usd, 6),
        "output_usd": round(output_usd, 6),
        "total_usd": round(input_usd + output_usd, 6),
    }
