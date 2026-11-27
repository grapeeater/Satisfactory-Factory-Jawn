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
from ..control import power
from ..control.actions import ActionKind, Proposal


def wanting_demand(ctx: TickContext, gid: int) -> float:
    """MW that paused, controlled machines on grid ``gid`` would add if they could run
    (not done, inputs available): demand the grid cannot see yet."""
    total = 0.0
    for alias, b in ctx.cfg.targets.items():
        if b.kind != "machine":
            continue
        m = ctx.machine(alias)
        if m is None or m.paused is not True or m.circuit_id != gid or ctx.done(alias) or not ctx.can_work(alias):
            continue
        if ctx.cfg.owner_of(alias) is None:
            continue
        total += m.max_power_mw
    return total


def switch_proposals(ctx: TickContext, aliases: list[str], source: str) -> list[Proposal]:
    """Deterministic power management for owned switches:
      1. close an open line tie when the merged grid can carry it (restores a shed line);
      2. close an open supply tie (backup generator) when a grid it would feed has more
         demand (running + waiting machines) than its own capacity allows;
      3. open a closed supply tie when everything fits comfortably without it (saves fuel).
    The arbiter re-checks every request against the pessimistic power model."""
    st, snap, cfg = ctx.st, ctx.snap, ctx.cfg
    margin = cfg.safety.power_margin
    out: list[Proposal] = []

    def mk(kind: ActionKind, alias: str, reason: str, observed: bool) -> Proposal:
        return Proposal(source, kind, alias, reason, snap.seq, snap.taken_at, ctx.now, observed_state=observed, priority=1.0)

    for alias in sorted(aliases):
        if cfg.targets[alias].kind != "switch" or alias in st.pending or st.locked(alias, ctx.now) or st.is_suspect(alias, ctx.now):
            continue
        sw = power.switch_obs(snap, st, alias)
        if sw is None or sw.on is None:
            continue
        kind = power.classify(st, alias)
        a, c = sw.circuits
        if sw.on is False:
            if kind == "line_tie":
                if power.check_switch_on(snap, st, alias, margin, {}) is None:
                    out.append(mk(ActionKind.SWITCH_ON, alias, "restore power to a production line", False))
            elif kind == "supply_tie":
                for g in (a, c):
                    circ = snap.circuits.get(g) if g is not None else None
                    if circ is None or circ.capacity <= 0:
                        continue
                    need = power.group_potential(snap, st, g) + wanting_demand(ctx, g)
                    if need > circ.capacity * (1.0 - margin):
                        out.append(mk(ActionKind.SWITCH_ON, alias, f"grid {g} needs {need:.0f} MW, has {circ.capacity:.0f} MW", False))
                        break
        elif kind == "supply_tie":
            gid = next((snap.machines[i].circuit_id for s in st.topology[alias].sides for i in s.machines if i in snap.machines), None)
            circ = snap.circuits.get(gid) if gid is not None else None
            if circ is None:
                continue
            need = power.group_potential(snap, st, gid) + wanting_demand(ctx, gid)
            without = circ.capacity - power.supply_capacity(st, alias)
            if without > 0 and need <= without * (1.0 - margin) * 0.9 and power.check_switch_off(snap, st, alias, margin) is None:
                out.append(mk(ActionKind.SWITCH_OFF, alias, f"demand {need:.0f} MW fits in {without:.0f} MW without backup", True))
    return out


def baseline_proposals(ctx: TickContext, aliases: list[str], source: str, weight: float = 1.0) -> list[Proposal]:
    st, snap, cfg = ctx.st, ctx.snap, ctx.cfg
    out: list[Proposal] = switch_proposals(ctx, [a for a in aliases if cfg.targets[a].kind == "switch"], source)
    aliases = [a for a in aliases if cfg.targets[a].kind == "machine"]

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
