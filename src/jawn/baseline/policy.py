"""Deterministic rule-based controller (the comparison baseline, and the budget fallback).

Rules, evaluated every tick in a fixed order:
  1. Disable a running machine whose target is met, whose output is full, or which is
     starved with nothing available upstream.
  2. Enable the paused machine with the largest remaining deficit (ties: alias order)
     that can work, if every constraint group it belongs to has a free slot under the
     pessimistic (pending-aware) count.
It produces Proposals that go through the same validator/arbiter/executor as agents.
"""

from __future__ import annotations

from ..context import TickContext, priority_for
from ..control.actions import ActionKind, Proposal


def baseline_proposals(ctx: TickContext, aliases: list[str], source: str, weight: float = 1.0) -> list[Proposal]:
    st, snap, cfg = ctx.st, ctx.snap, ctx.cfg
    out: list[Proposal] = []

    def mk(kind: ActionKind, alias: str, reason: str, observed: bool) -> Proposal:
        return Proposal(source, kind, alias, reason, snap.seq, snap.taken_at, ctx.now, observed_state=observed, priority=priority_for(ctx, alias, weight))

    usable = []
    for alias in sorted(aliases):
        m = ctx.machine(alias)
        if m is None or m.paused is None or alias in st.pending or st.locked(alias, ctx.now) or st.is_suspect(alias, ctx.now):
            continue
        usable.append(alias)
        if m.paused is False:
            if ctx.done(alias):
                out.append(mk(ActionKind.MACHINE_DISABLE, alias, "target reached", False))
            elif m.blocked:
                out.append(mk(ActionKind.MACHINE_DISABLE, alias, "output full", False))
            elif m.starved and not ctx.can_work(alias):
                out.append(mk(ActionKind.MACHINE_DISABLE, alias, "starved, nothing upstream", False))

    cands = [a for a in usable if ctx.machine(a).paused is True and not ctx.done(a) and ctx.can_work(a)]  # type: ignore[union-attr]
    cands.sort(key=lambda a: (-ctx.remaining_fraction(a), a))
    claimed: set[str] = set()
    for alias in cands:
        ok = True
        for c in cfg.constraints:
            if alias not in c.targets:
                continue
            running = {t for t in c.targets if st.running(snap, t, cfg.safety.count_pending_as_running)} | (claimed & set(c.targets))
            if len(running) >= c.max:
                ok = False
                break
        if ok:
            # only enable machines that actually have a target (or no target configured at all)
            out.append(mk(ActionKind.MACHINE_ENABLE, alias, f"largest deficit ({ctx.remaining_fraction(alias):.0%} remaining)", True))
            claimed.add(alias)
    return out


class BaselineController:
    name = "baseline"

    def __init__(self, aliases: list[str], source: str = "baseline") -> None:
        self.aliases = aliases
        self.source = source

    async def step(self, ctx: TickContext) -> list[Proposal]:
        return baseline_proposals(ctx, self.aliases, self.source)

    def on_result(self, proposal: Proposal, stage: str, code: str, detail: str) -> None:
        pass

    def summary(self) -> dict[str, object]:
        return {"controller": "baseline", "aliases": self.aliases}

    async def shutdown(self) -> None:
        pass
