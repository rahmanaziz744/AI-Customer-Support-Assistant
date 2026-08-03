"""Token pricing and cost arithmetic.

Prices are USD per million tokens, taken from the Anthropic pricing page. They
live in code (rather than being fetched) so a cost figure is reproducible for a
given commit; `UNKNOWN_MODEL_COST` keeps an unrecognised model from silently
reporting $0.
"""

from dataclasses import dataclass
from decimal import Decimal

# USD per 1M tokens: (input, output)
MODEL_PRICING: dict[str, tuple[Decimal, Decimal]] = {
    "claude-opus-5": (Decimal("5.00"), Decimal("25.00")),
    "claude-fable-5": (Decimal("10.00"), Decimal("50.00")),
    "claude-opus-4-8": (Decimal("5.00"), Decimal("25.00")),
    "claude-opus-4-7": (Decimal("5.00"), Decimal("25.00")),
    "claude-opus-4-6": (Decimal("5.00"), Decimal("25.00")),
    "claude-sonnet-5": (Decimal("3.00"), Decimal("15.00")),
    "claude-sonnet-4-6": (Decimal("3.00"), Decimal("15.00")),
    "claude-haiku-4-5": (Decimal("1.00"), Decimal("5.00")),
}

# Cache reads bill at ~0.1x input, cache writes at ~1.25x input.
CACHE_READ_MULTIPLIER = Decimal("0.1")
CACHE_WRITE_MULTIPLIER = Decimal("1.25")

_MILLION = Decimal("1000000")


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
        )

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )


class UnknownModelPricing(LookupError):
    """Raised when a model has no entry in MODEL_PRICING."""


def _rates(model: str) -> tuple[Decimal, Decimal]:
    if model in MODEL_PRICING:
        return MODEL_PRICING[model]
    # Tolerate dated snapshots such as "claude-haiku-4-5-20251001".
    for known, rates in MODEL_PRICING.items():
        if model.startswith(known):
            return rates
    raise UnknownModelPricing(model)


def calculate_cost(model: str, usage: TokenUsage) -> Decimal:
    """Cost in USD for one model call. Rounded to 6dp — sub-cent calls matter here."""
    input_rate, output_rate = _rates(model)
    cost = (
        Decimal(usage.input_tokens) * input_rate
        + Decimal(usage.output_tokens) * output_rate
        + Decimal(usage.cache_read_tokens) * input_rate * CACHE_READ_MULTIPLIER
        + Decimal(usage.cache_write_tokens) * input_rate * CACHE_WRITE_MULTIPLIER
    ) / _MILLION
    return cost.quantize(Decimal("0.000001"))


def safe_calculate_cost(model: str, usage: TokenUsage) -> Decimal | None:
    """Like `calculate_cost`, but returns None for an unpriced model.

    Used on the tracing path, where an unrecognised model should leave the cost
    column empty rather than fail the request or record a misleading zero.
    """
    try:
        return calculate_cost(model, usage)
    except UnknownModelPricing:
        return None


def usage_from_anthropic(raw: dict | None) -> TokenUsage:
    """Normalise a LangChain/Anthropic `usage_metadata` dict into TokenUsage."""
    if not raw:
        return TokenUsage()
    details = raw.get("input_token_details") or {}
    cache_read = int(details.get("cache_read") or 0)
    cache_write = int(details.get("cache_creation") or 0)
    # LangChain reports input_tokens inclusive of cached tokens; split them out
    # so each bucket is billed at its own rate exactly once.
    input_tokens = max(int(raw.get("input_tokens") or 0) - cache_read - cache_write, 0)
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=int(raw.get("output_tokens") or 0),
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
    )
