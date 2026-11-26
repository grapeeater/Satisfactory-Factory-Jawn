"""DeepSeek pricing (USD per 1M tokens), peak/off-peak aware. Values live in config."""

from __future__ import annotations

from datetime import datetime, timezone

from ..config import PricingConfig
from .base import Usage


def is_peak(when: datetime, p: PricingConfig) -> bool:
    when = when.astimezone(timezone.utc)
    if p.peak_weekdays_only and when.weekday() >= 5:
        return False
    h = when.hour + when.minute / 60.0
    return any(a <= h < b for a, b in p.peak_windows_utc)


def factor(when: datetime, p: PricingConfig) -> float:
    return 1.0 if is_peak(when, p) else p.offpeak_factor


def cost(usage: Usage, p: PricingConfig, when: datetime | None = None) -> float:
    when = when or datetime.now(timezone.utc)
    f = factor(when, p)
    hit = usage.cache_hit_tokens
    miss = usage.cache_miss_tokens if (usage.cache_hit_tokens or usage.cache_miss_tokens) else usage.prompt_tokens
    return f * (hit * p.input_hit + miss * p.input_miss + usage.completion_tokens * p.output) / 1_000_000


def worst_case(prompt_tokens: int, max_tokens: int, p: PricingConfig) -> float:
    """Upper bound used for pre-call reservation: all input uncached, full output, peak price."""
    return (prompt_tokens * p.input_miss + max_tokens * p.output) / 1_000_000
