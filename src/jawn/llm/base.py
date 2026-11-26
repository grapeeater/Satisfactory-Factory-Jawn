"""Provider-neutral model interface."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol


@dataclass
class Usage:
    prompt_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0
    simulated: bool = False  # True for mock/scripted/replay (no money spent)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["cost_usd"] = round(self.cost_usd, 8)
        return d


@dataclass
class DecisionRequest:
    agent_id: str
    messages: list[dict[str, str]]
    tool: dict[str, Any]  # function schema for the structured decision
    view: dict[str, Any]  # structured view (used only by offline providers; never sent to a paid API besides as text)
    call_index: int
    max_tokens: int = 200
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderResult:
    raw: str  # tool-call arguments or message content, exactly as returned
    usage: Usage
    model: str
    finish_reason: str = "stop"
    simulated_latency_s: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)


class ProviderError(Exception):
    """kind: budget | auth | rate_limit | server | timeout | bad_request | paid_calls_disabled | fatal"""

    def __init__(self, kind: str, detail: str, retryable: bool = False, status: int | None = None) -> None:
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail
        self.retryable = retryable
        self.status = status


class Provider(Protocol):
    name: str
    paid: bool

    async def complete(self, req: DecisionRequest) -> ProviderResult: ...

    def estimate_max_cost(self, req: DecisionRequest) -> float: ...

    async def close(self) -> None: ...


def approx_tokens(text: str) -> int:
    """Rough token estimate (~3.5 chars/token for English+JSON). Used for budgeting only."""
    return max(1, int(len(text) / 3.5) + 1)
