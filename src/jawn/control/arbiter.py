"""Deterministic conflict arbitration (no model in the loop).

Order of authority: human state > safety (power, fuses) > constraints (e.g. max_running
groups) > need-based priority (deficit toward target, weighted) > longest-waiting > id.

* Load-reducing actions (disable / switch off) are always granted once validated.
* Load-increasing actions are granted one by one against a *projected* state that counts
  pending commands pessimistically, so a constraint can never be exceeded by in-flight
  writes (an enable waits until a peer's disable has been verified).
* The arbiter never "fixes" a violation caused by a human; it reports it and refuses
  further increases in that group.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..config import ConstraintConfig, SafetyConfig
from ..observe.snapshot import Snapshot
from .actions import SPECS, ActionKind, Proposal
from .state import ControlState


@dataclass
class Denial:
    proposal: Proposal
    code: str
    detail: str


@dataclass
class ArbitrationOutcome:
    grants: list[Proposal] = field(default_factory=list)
    denials: list[Denial] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    violations: list[dict[str, Any]] = field(default_factory=list)


class Arbiter:
    def __init__(self, constraints: list[ConstraintConfig], safety: SafetyConfig) -> None:
        self.constraints = constraints
        self.safety = safety
        self._waiting_since: dict[tuple[str, str, str], float] = {}
        self._reported_violations: set[tuple[str, frozenset[str]]] = set()

    def _running_set(self, c: ConstraintConfig, snap: Snapshot, st: ControlState, extra: set[str]) -> set[str]:
        return {a for a in c.targets if a in extra or st.running(snap, a, self.safety.count_pending_as_running)}

    def check_violations(self, snap: Snapshot, st: ControlState) -> list[dict[str, Any]]:
        out = []
        for c in self.constraints:
            running = {a for a in c.targets if st.observe(snap, a) is False}
            key = (c.name, frozenset(running))
            if len(running) > c.max:
                if key not in self._reported_violations:
                    self._reported_violations.add(key)
                    human = sorted(a for a in running if not st.harness_set.get(a, False))
                    out.append({"constraint": c.name, "running": sorted(running), "max": c.max, "caused_by_human": human, "action": "none (human priority); increases refused"})
            else:
                self._reported_violations = {k for k in self._reported_violations if k[0] != c.name}
        return out

    def arbitrate(self, proposals: list[Proposal], snap: Snapshot, st: ControlState, now: float) -> ArbitrationOutcome:
        out = ArbitrationOutcome(violations=self.check_violations(snap, st))
        decreases = [p for p in proposals if not SPECS[p.kind].increases_load]
        increases = [p for p in proposals if SPECS[p.kind].increases_load]
        out.grants.extend(decreases)

        def key(p: Proposal) -> tuple[Any, ...]:
            k = (p.source, p.target, p.kind.value)
            return (0 if p.system else 1, -p.priority, self._waiting_since.get(k, now), p.source, p.target)

        increases.sort(key=key)
        granted_enables: set[str] = set()
        added_power: dict[int, float] = {}
        contenders: dict[str, list[Proposal]] = {}
        for p in increases:
            for c in self.constraints:
                if p.target in c.targets:
                    contenders.setdefault(c.name, []).append(p)

        for p in increases:
            k = (p.source, p.target, p.kind.value)
            code, detail = self._check_increase(p, snap, st, granted_enables, added_power)
            if code is None:
                out.grants.append(p)
                granted_enables.add(p.target)
                self._waiting_since.pop(k, None)
            else:
                self._waiting_since.setdefault(k, now)
                out.denials.append(Denial(p, code, detail))

        # conflict records: (a) same-cycle contention between different sources in a group,
        # (b) a request denied because a slot is occupied by another source's machine.
        in_contention: set[str] = set()
        for cname, ps in contenders.items():
            if len(ps) < 2 or len({p.source for p in ps}) < 2:
                continue
            winners = [p for p in ps if p in out.grants]
            losers = [p for p in ps if p not in out.grants]
            if losers:
                in_contention.update(p.id for p in ps)
                out.conflicts.append(
                    {
                        "constraint": cname,
                        "kind": "contention",
                        "winner": [[p.source, p.target, round(p.priority, 4)] for p in winners],
                        "losers": [[p.source, p.target, round(p.priority, 4)] for p in losers],
                        "basis": "priority (deficit x weight), then longest waiting, then id",
                    }
                )
        for d in out.denials:
            if d.code == "CONSTRAINT_FULL" and d.proposal.id not in in_contention:
                out.conflicts.append({"constraint": d.detail.split(":")[0], "kind": "occupied", "denied": [d.proposal.source, d.proposal.target], "detail": d.detail})
        return out

    def _check_increase(self, p: Proposal, snap: Snapshot, st: ControlState, granted: set[str], added_power: dict[int, float]) -> tuple[str | None, str]:
        for c in self.constraints:
            if p.target not in c.targets:
                continue
            running = self._running_set(c, snap, st, granted) - {p.target}
            if len(running) >= c.max:
                holders = sorted(running)
                return "CONSTRAINT_FULL", f"{c.name}: {len(running)}/{c.max} running ({', '.join(holders)})"
        if p.kind == ActionKind.MACHINE_ENABLE:
            m = snap.machines.get(st.resolved.get(p.target, ""))
            if m is not None and m.circuit_id is not None:
                circ = snap.circuits.get(m.circuit_id)
                if circ is None:
                    return "POWER_UNKNOWN", f"no power data for circuit {m.circuit_id}"
                if circ.fuse:
                    return "POWER_FUSE", f"circuit {circ.id} fuse is tripped; a human must reset it"
                need = m.max_power_mw or 0.0
                pending_extra = sum(
                    (snap.machines[st.resolved[a]].max_power_mw if st.resolved.get(a) in snap.machines else 0.0)
                    for a, cmd in st.pending.items()
                    if cmd.attr == "paused" and cmd.value is False and st.resolved.get(a) in snap.machines and snap.machines[st.resolved[a]].circuit_id == circ.id
                )
                projected = circ.consumed + pending_extra + added_power.get(circ.id, 0.0) + need
                limit = circ.capacity * (1.0 - self.safety.power_margin)
                if projected > limit:
                    return "POWER_HEADROOM", f"circuit {circ.id}: projected {projected:.1f} MW > {limit:.1f} MW allowed"
                added_power[circ.id] = added_power.get(circ.id, 0.0) + need
        return None, ""
