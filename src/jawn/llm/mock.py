"""Offline providers (no network, no spend).

* MockProvider    - heuristic "model" with personalities, reading the structured view.
                    Simulates token usage and prefix-cache hits so cost/budget logic can be
                    exercised and estimated offline (usage.simulated = True).
* ScriptedProvider - returns predetermined raw outputs / errors per agent (failure injection).
* ReplayProvider   - replays raw outputs recorded in a previous run's event log.
"""

from __future__ import annotations

import json
import random
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..config import PricingConfig
from . import pricing
from .base import DecisionRequest, ProviderError, ProviderResult, Usage, approx_tokens


def _request_text(req: DecisionRequest) -> str:
    return json.dumps(req.tool, sort_keys=True) + "".join(m["content"] for m in req.messages)


class _UsageSim:
    """Approximate DeepSeek-style prefix caching: the longest common prefix with any of the
    last few requests is a hit, rounded down to 64-token units."""

    def __init__(self, price: PricingConfig) -> None:
        self.price = price
        self.recent: deque[str] = deque(maxlen=16)

    def usage(self, req: DecisionRequest, output: str) -> Usage:
        text = _request_text(req)
        best = 0
        for prev in self.recent:
            n = 0
            for a, b in zip(prev, text):
                if a != b:
                    break
                n += 1
            best = max(best, n)
        self.recent.append(text)
        total = approx_tokens(text)
        hit = min(total, (int(best / 3.5) // 64) * 64)
        u = Usage(prompt_tokens=total, cache_hit_tokens=hit, cache_miss_tokens=total - hit, completion_tokens=approx_tokens(output), simulated=True)
        u.cost_usd = pricing.cost(u, self.price, SIM_PRICE_TIME)  # fixed peak-hour price: deterministic, conservative
        return u


# A weekday peak hour (Mon 2026-11-23 02:00 UTC): simulated costs are always priced at peak.
SIM_PRICE_TIME = datetime(2026, 11, 23, 2, 0, tzinfo=timezone.utc)


def _decision(action: str, target: str, reason: str, memory: str, to: str = "none", message: str = "") -> str:
    return json.dumps({"action": action, "target": target or "none", "reason": reason, "message_to": to, "message": message, "memory_update": memory})


def _power_policy(view: dict[str, Any]) -> str:
    """Mock power manager: restore shed lines, bring a backup supply in under pressure (a
    peer asked for power, or a grid is near its limit), take it out again when not needed."""
    sws = [s for s in view.get("your_switches", []) if s.get("state") in ("on", "off")]
    grid = view.get("power", [])
    memory = view.get("your_memory") or ""
    inbox = view.get("messages_to_you", [])
    results = view.get("results_since_last_decision", [])
    asked = [m["from"] for m in inbox if "power" in m.get("message", "").lower()]
    pressure = bool(asked) or any(p["capacity_mw"] > 0 and p["potential_mw"] > 0.85 * p["capacity_mw"] for p in grid)
    mem = "pressure" if pressure else "calm"
    fused = [p["circuit"] for p in grid if p.get("fuse_tripped")]
    if fused:
        if "fuse advised" in memory:
            return _decision("wait", "none", "fuse tripped; waiting for the human", memory)
        return _decision("wait", "none", "fuse tripped; only the human can reset it", "fuse advised", "human", f"Fuse tripped on circuit {fused[0]}. Please reset it in game.")
    denied = {r.get("target") for r in results if "POWER" in str(r.get("outcome", ""))}
    usable = [s for s in sws if not s.get("pending_command") and not s.get("human_lock_s")]
    for s in usable:
        if s["state"] == "off" and s.get("kind") == "line_tie" and s["alias"] not in denied:
            return _decision("switch_on", s["alias"], "restore power to a production line", mem)
    line_blocked = any(s["state"] == "off" and s.get("kind") == "line_tie" for s in sws)
    for s in usable:
        if s["state"] == "off" and s.get("kind") == "supply_tie" and (pressure or line_blocked):
            to = asked[-1] if asked else "none"
            return _decision("switch_on", s["alias"], "bring backup supply in: demand is near capacity", mem, to, "Bringing backup power online." if asked else "")
    for s in usable:
        if s["state"] == "on" and s.get("kind") == "supply_tie" and not pressure and "calm" in memory:
            loads = [p for p in grid if p["potential_mw"] > 0]
            spare = all(p["potential_mw"] <= 0.6 * (p["capacity_mw"] - s.get("supply_mw", 0.0)) for p in loads)
            if loads and spare:
                return _decision("switch_off", s["alias"], "backup not needed; saving fuel", mem)
    return _decision("wait", "none", "power is fine", mem)


def _policy(view: dict[str, Any], agent_id: str, personality: str, rng: random.Random) -> str:
    if personality == "power":
        return _power_policy(view)
    mine = [m for m in view["your_machines"] if m.get("state") != "missing"]
    if not mine:
        remaining = sum(p["remaining"] for p in view["progress"])
        return _decision("wait", "none", "my machines are not visible", f"remaining {remaining}")
    if personality in ("sleepy", "erratic") or len(mine) == 1:
        return _machine_policy(view, mine[0], personality, rng)
    # several machines: act on the first that needs an action; otherwise the first message-
    # carrying wait; otherwise a plain wait
    decisions = [json.loads(_machine_policy(view, m, personality, rng)) for m in mine]
    act = next((d for d in decisions if d["action"] != "wait"), None) or next((d for d in decisions if d["message_to"] != "none"), None) or decisions[0]
    return json.dumps(act)


def _power_agent(view: dict[str, Any]) -> str:
    owners = {o["owner"] for o in view.get("other_machines", []) if o.get("state") in ("on", "off")}
    return sorted(owners)[0] if owners else "none"


def _machine_policy(view: dict[str, Any], m: dict[str, Any], personality: str, rng: random.Random) -> str:
    remaining = sum(p["remaining"] for p in view["progress"])
    done = bool(view["progress"]) and remaining == 0
    mem = f"remaining {remaining}"
    alias, state = m["alias"], m["state"]
    if personality == "sleepy":
        return _decision("wait", "none", "observing only", mem)
    if personality == "erratic":
        roll = rng.random()
        if roll < 0.15:
            return _decision("enable", "not_my_machine", "chaos", mem)
        return _decision(rng.choice(["enable", "disable", "wait", "wait"]), alias, "random walk", mem)
    if m.get("human_lock_s"):
        return _decision("wait", "none", f"{alias} locked by human", mem + "; human lock seen")
    if m.get("pending_command"):
        return _decision("wait", "none", "last command still being verified", mem)
    fuse = any(p.get("fuse_tripped") for p in view.get("power", []))
    up_empty = all(v == 0 for v in m.get("upstream_stock", {}).values()) if m.get("upstream_stock") else False
    groups = view.get("constraints", [])
    full = [g for g in groups if alias not in g["running"] and len(g["running"]) >= g["max_running"]]
    inbox = view.get("messages_to_you", [])
    denied_recently = any("CONSTRAINT_FULL" in str(r.get("outcome", "")) for r in view.get("results_since_last_decision", []))
    power_denied = any(r.get("target") == alias and "POWER_HEADROOM" in str(r.get("outcome", "")) for r in view.get("results_since_last_decision", []))

    if state != "paused":  # running in some condition
        if done:
            return _decision("disable", alias, "target reached; freeing the slot", mem + "; done")
        if personality == "greedy":
            return _decision("wait", "none", "keep running", mem)
        if state == "output_full":
            return _decision("disable", alias, "output buffer full", mem)
        if state == "starved" and up_empty:
            return _decision("disable", alias, "no input available upstream", mem)
        if inbox and personality == "cooperative":
            asker = inbox[-1]["from"]
            return _decision("wait", "none", "still short of target", mem, asker, f"I need {remaining} more; will disable as soon as I reach my target.")
        return _decision("wait", "none", "producing toward target", mem)

    # paused
    if done:
        return _decision("wait", "none", "target already reached", mem + "; done")
    if fuse:
        return _decision("wait", "none", "fuse tripped; waiting for human reset", mem + "; fuse")
    if up_empty and m.get("input_buffer") and all(v == 0 for v in m["input_buffer"].values()):
        return _decision("wait", "none", "no input available", mem)
    if full and personality != "greedy":
        holders = full[0]["running"]
        owners = {o["alias"]: o["owner"] for o in view.get("other_machines", [])}
        holder_agent = next((owners[h] for h in holders if h in owners), "none")
        already_asked = "asked" in (view.get("your_memory") or "")
        if personality == "cooperative" and holder_agent != "none" and not already_asked:
            return _decision("wait", "none", f"slot held by {', '.join(holders)}", mem + "; asked holder", holder_agent, f"I still need {remaining}. Please disable when you reach your target.")
        return _decision("wait", "none", f"slot held by {', '.join(holders)}", mem + ("; asked holder" if already_asked else ""))
    if denied_recently and personality == "cooperative":
        return _decision("wait", "none", "was denied; waiting for the slot", mem)
    if power_denied and personality == "cooperative":
        pa = _power_agent(view)
        if pa != "none":
            return _decision("wait", "none", "denied for power headroom", mem + "; asked for power", pa, f"Need {m.get('power_mw', 0)} MW of power for {alias} ({remaining} remaining).")
        return _decision("wait", "none", "denied for power headroom", mem)
    if state == "paused" and m.get("circuit") is not None:
        circ = next((p for p in view.get("power", []) if p["circuit"] == m["circuit"]), None)
        if circ and circ["capacity_mw"] > 0 and circ["potential_mw"] + m.get("power_mw", 0) > 0.95 * circ["capacity_mw"] and "asked for power" in (view.get("your_memory") or ""):
            return _decision("wait", "none", "not enough power headroom yet", mem + "; asked for power")
    return _decision("enable", alias, f"{remaining} remaining and a slot is free", mem)


class MockProvider:
    name = "mock"
    paid = False

    def __init__(self, price: PricingConfig, latency_s: float = 2.0, seed: int = 0, simulate_usage: bool = True, max_tokens: int = 200) -> None:
        self.latency_s = latency_s
        self.seed = seed
        self.simulate_usage = simulate_usage
        self.price = price
        self.max_tokens = max_tokens
        self._usage = _UsageSim(price)
        self._invalid_cycle: dict[str, int] = {}

    def estimate_max_cost(self, req: DecisionRequest) -> float:
        return pricing.worst_case(approx_tokens(_request_text(req)), req.max_tokens, self.price) if self.simulate_usage else 0.0

    async def complete(self, req: DecisionRequest) -> ProviderResult:
        personality = str(req.meta.get("personality", "cooperative"))
        rng = random.Random(f"{self.seed}:{req.agent_id}:{req.call_index}")
        if personality == "invalid":
            i = self._invalid_cycle.get(req.agent_id, 0)
            self._invalid_cycle[req.agent_id] = i + 1
            raw = ["", "I think we should enable the machine!", '{"action":"explode","target":"none"}', '```json\n' + _decision("wait", "none", "fenced", "m") + "\n```"][i % 4]
        else:
            raw = _policy(req.view, req.agent_id, personality, rng)
        usage = self._usage.usage(req, raw) if self.simulate_usage else Usage(simulated=True)
        return ProviderResult(raw=raw, usage=usage, model=f"mock-{personality}", simulated_latency_s=self.latency_s)

    async def close(self) -> None:
        pass


Script = str | ProviderError | Callable[[DecisionRequest], str]


class ScriptedProvider:
    name = "scripted"
    paid = False

    def __init__(self, scripts: dict[str, list[Script]], latency_s: float = 0.0, default: str | None = None) -> None:
        self.scripts = {k: list(v) for k, v in scripts.items()}
        self.latency_s = latency_s
        self.default = default if default is not None else _decision("wait", "none", "script exhausted", "")
        self.requests: list[DecisionRequest] = []

    def estimate_max_cost(self, req: DecisionRequest) -> float:
        return 0.0

    async def complete(self, req: DecisionRequest) -> ProviderResult:
        self.requests.append(req)
        q = self.scripts.get(req.agent_id) or []
        item: Script = q.pop(0) if q else self.default
        if isinstance(item, ProviderError):
            raise item
        raw = item(req) if callable(item) else item
        return ProviderResult(raw=raw, usage=Usage(prompt_tokens=approx_tokens(_request_text(req)), completion_tokens=approx_tokens(raw), simulated=True), model="scripted", simulated_latency_s=self.latency_s)

    async def close(self) -> None:
        pass


class ReplayProvider:
    """Replays model outputs from a previous run (events.jsonl), per agent, in call order."""

    name = "replay"
    paid = False

    def __init__(self, events_path: str | Path, latency_s: float = 0.0) -> None:
        self.latency_s = latency_s
        self.queues: dict[str, deque[str]] = {}
        with open(events_path, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                ev = json.loads(line)
                if ev.get("type") == "llm_response":
                    self.queues.setdefault(ev["agent"], deque()).append(ev.get("raw", ""))

    def estimate_max_cost(self, req: DecisionRequest) -> float:
        return 0.0

    async def complete(self, req: DecisionRequest) -> ProviderResult:
        q = self.queues.get(req.agent_id)
        raw = q.popleft() if q else _decision("wait", "none", "replay exhausted", "")
        return ProviderResult(raw=raw, usage=Usage(simulated=True), model="replay", simulated_latency_s=self.latency_s)

    async def close(self) -> None:
        pass
