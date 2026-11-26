"""Spend limits with pre-call reservation.

Before every call the worst-case cost is reserved; if spent + reserved + worst_case would
exceed the cap, the call is refused *before* any request is made. After the call, actual
cost (from the provider's usage fields) replaces the reservation.

Real and simulated usage are accounted separately: a mock run can exhaust its simulated
budget (to exercise fallback logic) without implying any spend.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .base import Usage


class BudgetExceeded(Exception):
    pass


@dataclass
class Ledger:
    spent_usd: float = 0.0
    reserved_usd: float = 0.0
    calls: int = 0
    prompt_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    completion_tokens: int = 0


@dataclass
class BudgetMeter:
    max_usd: float  # real money cap
    simulated_max_usd: float
    max_calls: int
    max_calls_per_agent: int
    real: Ledger = field(default_factory=Ledger)
    simulated: Ledger = field(default_factory=Ledger)
    per_agent_calls: dict[str, int] = field(default_factory=dict)
    per_agent_cost: dict[str, float] = field(default_factory=dict)
    refused: int = 0
    hard_stop: str | None = None  # e.g. provider said 402 insufficient balance

    def _ledger(self, simulated: bool) -> tuple[Ledger, float]:
        return (self.simulated, self.simulated_max_usd) if simulated else (self.real, self.max_usd)

    def total_calls(self) -> int:
        return self.real.calls + self.simulated.calls

    def reserve(self, agent: str, worst_usd: float, simulated: bool) -> float:
        if self.hard_stop:
            self.refused += 1
            raise BudgetExceeded(self.hard_stop)
        if self.total_calls() >= self.max_calls:
            self.refused += 1
            raise BudgetExceeded(f"run call cap reached ({self.max_calls})")
        if self.per_agent_calls.get(agent, 0) >= self.max_calls_per_agent:
            self.refused += 1
            raise BudgetExceeded(f"agent call cap reached ({self.max_calls_per_agent})")
        led, cap = self._ledger(simulated)
        if led.spent_usd + led.reserved_usd + worst_usd > cap + 1e-12:
            self.refused += 1
            kind = "simulated" if simulated else "real"
            raise BudgetExceeded(f"{kind} budget: spent ${led.spent_usd:.6f} + reserved ${led.reserved_usd:.6f} + worst case ${worst_usd:.6f} > cap ${cap:.6f}")
        led.reserved_usd += worst_usd
        self.per_agent_calls[agent] = self.per_agent_calls.get(agent, 0) + 1
        return worst_usd

    def commit(self, agent: str, reservation: float, usage: Usage | None) -> None:
        led, _ = self._ledger(usage.simulated if usage else False)
        led.reserved_usd = max(0.0, led.reserved_usd - reservation)
        if usage is None:
            return
        led.spent_usd += usage.cost_usd
        led.calls += 1
        led.prompt_tokens += usage.prompt_tokens
        led.cache_hit_tokens += usage.cache_hit_tokens
        led.cache_miss_tokens += usage.cache_miss_tokens
        led.completion_tokens += usage.completion_tokens
        self.per_agent_cost[agent] = self.per_agent_cost.get(agent, 0.0) + usage.cost_usd

    def release(self, agent: str, reservation: float, simulated: bool) -> None:
        """Call failed without usage (e.g. connection error): drop the reservation."""
        led, _ = self._ledger(simulated)
        led.reserved_usd = max(0.0, led.reserved_usd - reservation)

    def summary(self) -> dict[str, Any]:
        def led(l: Ledger, cap: float) -> dict[str, Any]:
            hit_ratio = l.cache_hit_tokens / max(1, l.cache_hit_tokens + l.cache_miss_tokens)
            return {"spent_usd": round(l.spent_usd, 8), "cap_usd": cap, "calls": l.calls, "prompt_tokens": l.prompt_tokens, "cache_hit_tokens": l.cache_hit_tokens, "cache_miss_tokens": l.cache_miss_tokens, "completion_tokens": l.completion_tokens, "cache_hit_ratio": round(hit_ratio, 4)}

        return {
            "real": led(self.real, self.max_usd),
            "simulated": led(self.simulated, self.simulated_max_usd),
            "per_agent_calls": dict(self.per_agent_calls),
            "per_agent_cost_usd": {k: round(v, 8) for k, v in self.per_agent_cost.items()},
            "refused": self.refused,
            "hard_stop": self.hard_stop,
            "max_calls": self.max_calls,
        }
