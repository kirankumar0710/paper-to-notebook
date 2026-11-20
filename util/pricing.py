"""Estimate what a conversion cost from its token counts.

Prices come from the ``[pricing]`` section of the config (see config.ini):
``<model id> = <input>, <output>`` in USD per million tokens at standard
speed, and ``fast_multiplier`` for fast mode.
"""

from __future__ import annotations

import configparser
from typing import Any

from util.config import load_config
from util.log import get_logger

__all__ = ["estimate_cost", "load_prices"]

SECTION = "pricing"
FAST_MULTIPLIER_KEY = "fast_multiplier"
TOKENS_PER_PRICE_UNIT = 1_000_000

logger = get_logger(__name__)


def load_prices(
    config: configparser.ConfigParser | None = None,
) -> tuple[dict[str, tuple[float, float]], float]:
    """Return ``({model: (input, output)}, fast_multiplier)``.

    Lines that cannot be parsed are skipped with a warning.
    """
    if config is None:
        config = load_config()
    if not config.has_section(SECTION):
        return {}, 1.0
    section = config[SECTION]
    try:
        multiplier = float(section.get(FAST_MULTIPLIER_KEY, "1"))
    except ValueError:
        logger.warning("Ignoring invalid [pricing] fast_multiplier")
        multiplier = 1.0
    prices: dict[str, tuple[float, float]] = {}
    for model, value in section.items():
        if model == FAST_MULTIPLIER_KEY:
            continue
        try:
            input_price, output_price = (
                float(part) for part in value.split(",")
            )
        except ValueError:
            logger.warning("Ignoring invalid [pricing] line for %s", model)
            continue
        prices[model] = (input_price, output_price)
    return prices, multiplier


def estimate_cost(
    model: str,
    usage: dict[str, int] | None,
    *,
    speed: str | None = None,
    config: configparser.ConfigParser | None = None,
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
