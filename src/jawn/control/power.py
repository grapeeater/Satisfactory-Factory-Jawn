"""Pessimistic power model over FRM observations (shared by arbiter, agent views, baseline).

FRM reports power per circuit *group* (CircuitGroupID) and, for switches, the two circuits a
switch connects (Connected0/1). It does not say which side of a closed switch a machine or
generator sits on, so the harness learns each switch's two sides while it is observed open
(capacity and machine IDs per side) and uses that to judge whether opening it is safe.

Demand is counted pessimistically: every machine that is not known to be paused counts at its
maximum power (an idle machine can start a cycle at any moment), plus pending enables.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..observe.snapshot import Snapshot

if TYPE_CHECKING:
    from .state import ControlState


@dataclass
class Side:
    circuit: int
    capacity: float
    machines: set[str] = field(default_factory=set)


@dataclass
class SwitchTopology:
    sides: tuple[Side, Side]
    learned_at_seq: int


def running_ids(snap: Snapshot, st: ControlState) -> set[str]:
    pend_on = {st.resolved.get(a) for a, c in st.pending.items() if c.attr == "paused" and c.value is False}
    return {m.id for m in snap.machines.values() if m.paused is not True} | {i for i in pend_on if i}


def group_potential(snap: Snapshot, st: ControlState, gid: int, run: set[str] | None = None) -> float:
    run = running_ids(snap, st) if run is None else run
    pot = sum(m.max_power_mw for m in snap.machines.values() if m.circuit_id == gid and m.id in run)
    c = snap.circuits.get(gid)
    return max(pot, c.consumed if c else 0.0)


def switch_obs(snap: Snapshot, st: ControlState, alias: str):  # -> SwitchObs | None
    return snap.switches.get(st.resolved.get(alias, ""))


def learn_topology(snap: Snapshot, st: ControlState) -> None:
    """Record both sides of every controlled switch that is currently open."""
    if not {"machines", "power", "switches"} <= snap.fresh:
        return
    for alias, b in st.bindings.items():
        if b.kind != "switch":
            continue
        sw = switch_obs(snap, st, alias)
        if sw is None or sw.on is not False:
            continue
        a, c = sw.circuits
        if a is None or c is None or a == c or a not in snap.circuits or c not in snap.circuits:
            continue
        sides = tuple(Side(g, snap.circuits[g].capacity, {m.id for m in snap.machines.values() if m.circuit_id == g}) for g in (a, c))
        st.topology[alias] = SwitchTopology(sides, snap.seq)  # type: ignore[arg-type]


def check_machine_enable(snap: Snapshot, st: ControlState, alias: str, margin: float, added: dict[int, float] | None = None) -> tuple[str, str] | None:
    """Would enabling this machine risk overloading its grid (pessimistic projection)?"""
    added = {} if added is None else added
    m = snap.machines.get(st.resolved.get(alias, ""))
    if m is None or m.circuit_id is None:
        return None
    circ = snap.circuits.get(m.circuit_id)
    if circ is None:
        return "POWER_UNKNOWN", f"no power data for circuit {m.circuit_id}"
    if circ.fuse:
        return "POWER_FUSE", f"circuit {circ.id} fuse is tripped; a human must reset it"
    if circ.capacity <= 0:
        # no generation on this grid (e.g. its line switch is open): enabling draws nothing
        # now; closing the switch later is checked with this machine counted
        return None
    need = m.max_power_mw or 0.0
    projected = group_potential(snap, st, circ.id) + added.get(circ.id, 0.0) + need
    limit = circ.capacity * (1.0 - margin)
    if projected > limit:
        return "POWER_HEADROOM", f"circuit {circ.id}: projected {projected:.1f} MW > {limit:.1f} MW allowed"
    added[circ.id] = added.get(circ.id, 0.0) + need
    return None


def check_switch_on(snap: Snapshot, st: ControlState, alias: str, margin: float, added: dict[int, float]) -> tuple[str, str] | None:
    sw = switch_obs(snap, st, alias)
    if sw is None:
        return "POWER_UNKNOWN", f"no observation for switch {alias}"
    a, c = sw.circuits
    if a is None or c is None or a not in snap.circuits or c not in snap.circuits:
        return "POWER_UNKNOWN", f"{alias}: circuits {sw.circuits} not in power data"
    if a == c:
        return None
    ca, cc = snap.circuits[a], snap.circuits[c]
    if ca.fuse or cc.fuse:
        return "POWER_FUSE", f"{alias}: a fuse is tripped on circuit {a if ca.fuse else c}; a human must reset it"
    cap = ca.capacity + cc.capacity
    if cap <= 0:
        return None  # no generation on either side: joining them draws nothing
    run = running_ids(snap, st)
    pot = group_potential(snap, st, a, run) + group_potential(snap, st, c, run) + added.get(a, 0.0) + added.get(c, 0.0)
    limit = cap * (1.0 - margin)
    if pot > limit:
        return "POWER_HEADROOM", f"{alias}: joining circuits {a}+{c} projects {pot:.1f} MW > {limit:.1f} MW allowed"
    return None


def check_switch_off(snap: Snapshot, st: ControlState, alias: str, margin: float) -> tuple[str, str] | None:
    """Opening a closed switch splits a grid. Refuse when either side that keeps generation
    would be left with more (pessimistic) demand than its own capacity."""
    sw = switch_obs(snap, st, alias)
    if sw is None:
        return "POWER_UNKNOWN", f"no observation for switch {alias}"
    topo: SwitchTopology | None = st.topology.get(alias)
    if topo is None:
        return "POWER_TOPOLOGY_UNKNOWN", f"{alias} has not been observed open; cannot tell what each side would carry"
    run = running_ids(snap, st)
    known = topo.sides[0].machines | topo.sides[1].machines
    merged_gid = next((snap.machines[i].circuit_id for i in known if i in snap.machines and snap.machines[i].circuit_id is not None), None)
    if merged_gid is None:
        merged_gid = next((s.circuit for s in topo.sides if s.circuit in snap.circuits), None)
    merged = snap.circuits.get(merged_gid) if merged_gid is not None else None
    if merged is None:
        return "POWER_UNKNOWN", f"{alias}: merged circuit not in power data"
    # machines now on the merged grid that were never seen on either side count on both sides
    unknown = {m.id for m in snap.machines.values() if m.circuit_id == merged_gid and m.id not in known}
    for i, s in enumerate(topo.sides):
        other = topo.sides[1 - i]
        # learned capacities go stale (a generator can fail after we learned the layout): what
        # this side keeps is at most the merged capacity now minus what the other side brought
        cap = max(0.0, min(s.capacity, merged.capacity - other.capacity))
        if s.capacity <= 0:
            continue  # a side without generation just goes dark; it cannot overload
        pot = sum(snap.machines[m].max_power_mw for m in (s.machines | unknown) if m in snap.machines and m in run)
        limit = cap * (1.0 - margin)
        if pot > limit:
            return "POWER_HEADROOM", f"{alias}: opening would leave circuit {s.circuit} with {pot:.1f} MW on {cap:.1f} MW"
    return None


def classify(st: ControlState, alias: str) -> str:
    """'supply_tie' (one side has generation and no machines, e.g. a backup generator),
    'line_tie' (one side has machines and no generation, e.g. a production line), or 'tie'."""
    topo: SwitchTopology | None = st.topology.get(alias)
    if topo is None:
        return "unknown"
    for s in topo.sides:
        if s.capacity > 0 and not s.machines:
            return "supply_tie"
    for s in topo.sides:
        if s.capacity <= 0 and s.machines:
            return "line_tie"
    return "tie"


def supply_capacity(st: ControlState, alias: str) -> float:
    topo: SwitchTopology | None = st.topology.get(alias)
    if topo is None:
        return 0.0
    return sum(s.capacity for s in topo.sides if s.capacity > 0 and not s.machines)


def grid_view(snap: Snapshot, st: ControlState) -> list[dict[str, object]]:
    run = running_ids(snap, st)
    return [
        {"circuit": cid, "capacity_mw": round(c.capacity, 1), "used_mw": round(c.consumed, 1), "potential_mw": round(group_potential(snap, st, cid, run), 1), "fuse_tripped": c.fuse}
        for cid, c in sorted(snap.circuits.items())
    ]
