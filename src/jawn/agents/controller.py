"""Event-driven model scheduling for a set of independent agents.

Each tick: collect finished model calls -> parse -> proposals/messages/memory; then wake
agents whose triggers fired (subject to min interval, concurrency cap and budget). Model
calls run as background tasks and never block observation or control.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, Callable

from ..baseline.policy import baseline_proposals
from ..clock import Clock
from ..config import Config
from ..context import TickContext, priority_for
from ..control.actions import ActionKind, Proposal, new_id
from ..llm.base import DecisionRequest, Provider, ProviderError, ProviderResult
from ..llm.budget import BudgetExceeded, BudgetMeter
from .agent import AgentRuntime
from .decision import decision_tool, parse_decision
from .prompt import build_messages

Emit = Callable[..., Any]

# outcomes that are fed back to the agent and wake it (it must adapt); verified results
# only update its history (the state change itself already wakes it)
FEEDBACK_STAGES = {"validation", "arbitration", "command", "verification_failed"}


class AgentController:
    name = "agents"

    def __init__(self, cfg: Config, provider: Provider, budget: BudgetMeter, clock: Clock, emit: Emit, mode: str = "tools_strict") -> None:
        self.cfg = cfg
        self.provider = provider
        self.budget = budget
        self.clock = clock
        self.emit = emit
        self.mode = mode
        ids = [a.id for a in cfg.agents]
        self.agents = {a.id: AgentRuntime(a, [i for i in ids if i != a.id]) for a in cfg.agents}
        self.owners = {alias: a.id for a in cfg.agents for alias in a.owns}
        self.allowed = {a.id: [ActionKind(k) for k in a.allowed_actions] for a in cfg.agents}
        self._decisions: dict[str, tuple[str, dict[str, Any]]] = {}  # decision_id -> (agent, history entry)

    # ------------------------------------------------------------------ main step
    async def step(self, ctx: TickContext) -> list[Proposal]:
        out: list[Proposal] = []
        for ag in sorted(self.agents.values(), key=lambda a: a.id):
            if ag.task is not None and ag.task.done():
                res = ag.task.result()
                lat = res[1].simulated_latency_s if res[0] == "ok" else 0.0
                if ctx.now >= ag.call_started + lat:
                    ag.task = None
                    out.extend(self._finish(ag, res, ctx))
        active = sum(1 for a in self.agents.values() if a.busy)
        for ag in sorted(self.agents.values(), key=lambda a: a.id):
            if ag.exhausted:
                if ag.cfg.on_budget_exhausted == "baseline":
                    for p in baseline_proposals(ctx, ag.cfg.owns, ag.id, ag.cfg.priority_weight):
                        p.reason = "[budget fallback] " + p.reason
                        out.append(p)
                continue
            view = ag.build_view(ctx, self.owners)
            reasons = ag.wake_reasons(ctx, view)
            if not reasons:
                continue
            if active >= self.cfg.llm.max_concurrent:
                ag.deferred += 1
                continue
            req = DecisionRequest(
                agent_id=ag.id,
                messages=build_messages(ag.cfg, ag.peers, view, self.mode),
                tool=decision_tool(self.allowed[ag.id], ag.cfg.owns, ag.peers),
                view=view,
                call_index=ag.calls,
                max_tokens=self.cfg.llm.deepseek.max_tokens,
                meta={"seq": ctx.snap.seq, "based_on_time": ctx.snap.taken_at, "observed": {m["alias"]: m.get("state") for m in view["your_machines"]}, "paused": {a: (ctx.machine(a).paused if ctx.machine(a) else None) for a in ag.cfg.owns}, "personality": ag.cfg.personality},
            )
            try:
                reservation = self.budget.reserve(ag.id, self.provider.estimate_max_cost(req), simulated=not self.provider.paid)
            except BudgetExceeded as e:
                ag.exhausted = str(e)
                self.emit("budget", agent=ag.id, event="exhausted", detail=str(e), fallback=ag.cfg.on_budget_exhausted, level="warn")
                continue
            ag.results.clear()
            ag.inbox.clear()
            ag.last_call_at = ctx.now
            ag.last_sig = ag.signature(view)
            ag.calls += 1
            ag.call_started = ctx.now
            ag.call_meta = req.meta
            ag.last_wake_reasons = reasons
            active += 1
            self.emit("agent_wake", agent=ag.id, reasons=reasons, call_index=req.call_index)
            self.emit("llm_request", agent=ag.id, provider=self.provider.name, call_index=req.call_index, messages=req.messages, tool=req.tool["function"]["name"])
            ag.task = asyncio.create_task(self._call(ag.id, req, reservation))
        return out

    async def _call(self, agent_id: str, req: DecisionRequest, reservation: float) -> tuple[str, Any]:
        try:
            res = await self.provider.complete(req)
        except ProviderError as e:
            self.budget.release(agent_id, reservation, simulated=not self.provider.paid)
            if e.kind == "budget":
                self.budget.hard_stop = f"provider reported: {e.detail}"
            return "error", e
        except asyncio.CancelledError:
            self.budget.release(agent_id, reservation, simulated=not self.provider.paid)
            raise
        except Exception as e:  # noqa: BLE001 - a provider bug must not kill the harness
            self.budget.release(agent_id, reservation, simulated=not self.provider.paid)
            return "error", ProviderError("fatal", f"{type(e).__name__}: {e}")
        self.budget.commit(agent_id, reservation, res.usage)
        return "ok", res

    def _finish(self, ag: AgentRuntime, res: tuple[str, Any], ctx: TickContext) -> list[Proposal]:
        meta = ag.call_meta
        if res[0] == "error":
            e: ProviderError = res[1]
            self.emit("llm_error", agent=ag.id, kind=e.kind, detail=e.detail, level="error")
            if e.kind in ("budget", "auth", "paid_calls_disabled", "fatal"):
                ag.exhausted = f"provider error: {e.kind}"
                self.emit("budget", agent=ag.id, event="disabled", detail=e.detail, fallback=ag.cfg.on_budget_exhausted, level="warn")
            if e.kind == "budget":
                for other in self.agents.values():
                    other.exhausted = other.exhausted or "provider reported insufficient balance"
            return []
        r: ProviderResult = res[1]
        self.emit("llm_response", agent=ag.id, call_index=ag.calls - 1, model=r.model, raw=r.raw, finish_reason=r.finish_reason, usage=r.usage.to_dict(), simulated_latency_s=r.simulated_latency_s, extra=r.extra)
        decision, err = parse_decision(r.raw, self.allowed[ag.id], ag.cfg.owns, ag.peers)
        if decision is None:
            ag.invalid += 1
            self.emit("invalid_output", agent=ag.id, error=err, raw=r.raw[:500], level="warn")
            ag.results.append({"decision": "invalid_output", "outcome": f"rejected: {err}"})
            return []
        ag.decisions += 1
        did = new_id("dec")
        if decision.memory_update:
            ag.memory = decision.memory_update
        entry = {"id": did, "t": int(ctx.now), "action": decision.action.value, "target": decision.target or "none", "reason": decision.reason, "outcome": "wait" if decision.action == ActionKind.WAIT else "proposed"}
        ag.history.append(entry)
        self._decisions[did] = (ag.id, entry)
        self.emit("decision", agent=ag.id, decision_id=did, based_on_seq=meta.get("seq"), **decision.to_dict())
        if decision.message_to:
            peer = self.agents.get(decision.message_to)
            if peer is not None:
                msg = {"from": ag.id, "t": int(ctx.now), "message": decision.message}
                peer.inbox.append(msg)
                self.emit("message", sender=ag.id, to=peer.id, message=decision.message, decision_id=did)
        if decision.action == ActionKind.WAIT:
            return []
        return [
            Proposal(
                source=ag.id,
                kind=decision.action,
                target=decision.target,
                reason=decision.reason,
                based_on_seq=int(meta.get("seq", 0)),
                based_on_time=float(meta.get("based_on_time", ctx.now)),
                created_at=ctx.now,
                observed_state=meta.get("paused", {}).get(decision.target),
                priority=priority_for(ctx, decision.target, ag.cfg.priority_weight),
                decision_id=did,
            )
        ]

    # ------------------------------------------------------------------ feedback
    def on_result(self, proposal: Proposal, stage: str, code: str, detail: str) -> None:
        ag = self.agents.get(proposal.source)
        if ag is None:
            return
        if proposal.decision_id and proposal.decision_id in self._decisions:
            self._decisions[proposal.decision_id][1]["outcome"] = f"{stage}:{code}"
        if stage in FEEDBACK_STAGES:
            ag.results.append({"decision_id": proposal.decision_id, "action": proposal.kind.value, "target": proposal.target, "outcome": f"{code}: {detail}"[:240]})

    def summary(self) -> dict[str, Any]:
        return {
            "controller": "agents",
            "provider": self.provider.name,
            "agents": {
                a.id: {
                    "role": a.cfg.role,
                    "owns": a.cfg.owns,
                    "calls": a.calls,
                    "decisions": a.decisions,
                    "invalid_outputs": a.invalid,
                    "deferred_wakes": a.deferred,
                    "busy": a.busy,
                    "exhausted": a.exhausted,
                    "memory": a.memory,
                    "last_wake_reasons": a.last_wake_reasons,
                    "history": list(a.history),
                    "inbox": list(a.inbox),
                }
                for a in self.agents.values()
            },
        }

    async def shutdown(self) -> None:
        for a in self.agents.values():
            if a.task is not None:
                a.task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await a.task
                a.task = None
        await self.provider.close()
