"""Deterministic factory simulation used in place of a live Satisfactory session.

Modelled behaviour (simplified but faithful to the mechanics the harness relies on):
  * Manufacturers consume a recipe's inputs at cycle start and deposit outputs at
    cycle end. Pausing (standby) or losing power freezes progress mid-cycle.
  * A cycle starts only if all inputs are buffered and every output fits.
  * Machine buffers hold one stack per ingredient/product; storage containers are
    slot-limited (stack sizes per item).
  * Belts move whole items at a rate limit; supplies model miners/upstream lines.
  * Circuits joined by switches form a grid. If demand exceeds generation, priority
    switches shed load (highest number first); otherwise the grid's fuse trips and
    stays tripped until a human resets it.
  * Game time only advances while the game is unpaused (single-player pause menu).

A truth timeline is recorded so tests can check constraints independently of the
harness' own beliefs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .catalog import BUILDINGS, ITEMS, RECIPES, RecipeDef, item

EPS = 1e-9


# --------------------------------------------------------------------------- inventories
@dataclass
class SlotInventory:
    """Storage container style: N slots, one item type per slot, stack-limited."""

    slots: int
    items: dict[str, int] = field(default_factory=dict)

    def count(self, cls: str) -> int:
        return self.items.get(cls, 0)

    def _used_slots(self) -> int:
        return sum(math.ceil(n / item(c).stack) for c, n in self.items.items() if n > 0)

    def room(self, cls: str) -> int:
        stack = item(cls).stack
        have = self.count(cls)
        partial = (stack - have % stack) % stack
        free = max(0, self.slots - self._used_slots())
        return partial + free * stack

    def add(self, cls: str, n: int) -> int:
        n = max(0, min(n, self.room(cls)))
        if n:
            self.items[cls] = self.count(cls) + n
        return n

    def take(self, cls: str, n: int) -> int:
        n = max(0, min(n, self.count(cls)))
        if n:
            self.items[cls] -= n
            if self.items[cls] == 0:
                del self.items[cls]
        return n

    def first_item(self) -> str | None:
        return next((c for c, n in sorted(self.items.items()) if n > 0), None)

    def snapshot(self) -> list[dict[str, Any]]:
        return [
            {"Name": item(c).name, "ClassName": c, "Amount": n, "MaxAmount": item(c).stack}
            for c, n in sorted(self.items.items())
            if n > 0
        ]


@dataclass
class BufferInventory:
    """Machine buffer: one slot per allowed item, capped at one stack."""

    caps: dict[str, int]
    items: dict[str, int] = field(default_factory=dict)

    def count(self, cls: str) -> int:
        return self.items.get(cls, 0)

    def room(self, cls: str) -> int:
        if cls not in self.caps:
            return 0
        return max(0, self.caps[cls] - self.count(cls))

    def add(self, cls: str, n: int) -> int:
        n = max(0, min(n, self.room(cls)))
        if n:
            self.items[cls] = self.count(cls) + n
        return n

    def take(self, cls: str, n: int) -> int:
        n = max(0, min(n, self.count(cls)))
        if n:
            self.items[cls] -= n
        return n

    def first_item(self) -> str | None:
        return next((c for c in sorted(self.caps) if self.count(c) > 0), None)

    def snapshot(self) -> list[dict[str, Any]]:
        return [
            {"Name": item(c).name, "ClassName": c, "Amount": self.count(c), "MaxAmount": cap}
            for c, cap in sorted(self.caps.items())
        ]


# --------------------------------------------------------------------------- entities
@dataclass
class Machine:
    id: str
    name: str
    class_name: str
    recipe: RecipeDef | None
    circuit: int
    location: tuple[float, float, float] = (0.0, 0.0, 0.0)
    paused: bool = False
    clock: float = 1.0
    power_mw: float = 4.0
    progress: float = 0.0
    cycle_active: bool = False
    producing: bool = False
    status: str = "idle"
    productivity: float = 0.0  # EMA of fraction of time producing, 0..1
    produced: dict[str, int] = field(default_factory=dict)
    consumed: dict[str, int] = field(default_factory=dict)
    inputs: BufferInventory = field(default_factory=lambda: BufferInventory({}))
    outputs: BufferInventory = field(default_factory=lambda: BufferInventory({}))

    def __post_init__(self) -> None:
        if self.recipe is not None and not self.inputs.caps and not self.outputs.caps:
            self.inputs = BufferInventory({c: item(c).stack for c in self.recipe.inputs})
            self.outputs = BufferInventory({c: item(c).stack for c in self.recipe.outputs})

    @property
    def duration(self) -> float:
        assert self.recipe is not None
        return self.recipe.duration_s / self.clock

    def can_start(self) -> bool:
        r = self.recipe
        if r is None:
            return False
        return all(self.inputs.count(c) >= n for c, n in r.inputs.items()) and all(
            self.outputs.room(c) >= n for c, n in r.outputs.items()
        )

    def wants_power(self) -> bool:
        return not self.paused and self.recipe is not None and (self.cycle_active or self.can_start())

    def step(self, dt: float, powered: bool) -> None:
        produced_time = 0.0
        r = self.recipe
        if r is None:
            self.status, self.producing = "unconfigured", False
        elif self.paused:
            self.status, self.producing = "paused", False
        elif not powered:
            self.status, self.producing = "unpowered", False
        else:
            t = dt
            self.producing = False
            while t > EPS:
                if not self.cycle_active:
                    if not all(self.inputs.count(c) >= n for c, n in r.inputs.items()):
                        self.status = "starved"
                        break
                    if not all(self.outputs.room(c) >= n for c, n in r.outputs.items()):
                        self.status = "blocked"
                        break
                    for c, n in r.inputs.items():
                        self.inputs.take(c, n)
                        self.consumed[c] = self.consumed.get(c, 0) + n
                    self.cycle_active, self.progress = True, 0.0
                need = self.duration - self.progress
                if t + EPS >= need:
                    t -= need
                    produced_time += need
                    for c, n in r.outputs.items():
                        self.outputs.add(c, n)
                        self.produced[c] = self.produced.get(c, 0) + n
                    self.cycle_active, self.progress = False, 0.0
                else:
                    self.progress += t
                    produced_time += t
                    t = 0.0
                self.producing = True
                self.status = "producing"
            if produced_time > EPS:
                self.producing = True
        frac = min(1.0, produced_time / dt) if dt > 0 else 0.0
        alpha = min(1.0, dt / 30.0)  # ~30 s smoothing like the in-game productivity readout
        self.productivity += (frac - self.productivity) * alpha


@dataclass
class StorageBox:
    id: str
    name: str
    class_name: str = "Build_StorageContainerMk1_C"
    location: tuple[float, float, float] = (0.0, 0.0, 0.0)
    inventory: SlotInventory = field(default_factory=lambda: SlotInventory(24))


@dataclass
class Belt:
    id: str
    src: str  # "<entity id>" (storage) or "<machine id>:out" / supply id
    dst: str  # "<entity id>" (storage) or "<machine id>:in"
    item: str
    rate_per_min: float = 60.0
    enabled: bool = True
    accum: float = 0.0
    moved: int = 0


@dataclass
class Supply:
    """Abstract upstream producer (e.g. miner + smelter line) pushing into a container/machine."""

    id: str
    item: str
    dst: str
    rate_per_min: float
    enabled: bool = True
    remaining: int | None = None  # None = infinite
    accum: float = 0.0


@dataclass
class Generator:
    id: str
    name: str
    circuit: int
    capacity_mw: float
    online: bool = True
    class_name: str = "Build_GeneratorCoal_C"
    fuel_s: float | None = None  # seconds of fuel at load; None = unlimited. Burns only while supplying load.
    fuel_used_s: float = 0.0


@dataclass
class PowerSwitch:
    id: str
    name: str
    a: int
    b: int
    on: bool = True
    priority: int = 0  # 0 = plain switch; 1..8 = priority switch (8 sheds first)
    class_name: str = "Build_PowerSwitch_C"


@dataclass
class Grid:
    id: int  # lowest circuit id in the grid (used as CircuitGroupID)
    circuits: frozenset[int]
    capacity: float
    demand: float
    max_consumed: float
    fuse: bool


# --------------------------------------------------------------------------- world
class World:
    def __init__(self, session_name: str = "JawnSim") -> None:
        self.session_name = session_name
        self.game_time = 0.0
        self.game_paused = False
        self.machines: dict[str, Machine] = {}
        self.storages: dict[str, StorageBox] = {}
        self.belts: list[Belt] = []
        self.supplies: dict[str, Supply] = {}
        self.generators: dict[str, Generator] = {}
        self.switches: dict[str, PowerSwitch] = {}
        self.fused: set[int] = set()  # circuits whose fuse is tripped
        self.chat: list[dict[str, Any]] = []
        self.timeline: list[dict[str, Any]] = []  # truth: state transitions
        self.events: list[dict[str, Any]] = []  # truth: power trips, sheds, human acts
        self._last_state: dict[str, tuple[bool, bool, str]] = {}
        self.grids: list[Grid] = []
        self._recompute_grids()

    # ---- building
    def add_machine(self, m: Machine) -> Machine:
        self.machines[m.id] = m
        self._recompute_grids()
        return m

    def add_storage(self, s: StorageBox) -> StorageBox:
        self.storages[s.id] = s
        return s

    # ---- endpoint resolution
    def _inv(self, ref: str, direction: str):
        if ref in self.storages:
            return self.storages[ref].inventory
        mid, _, port = ref.rpartition(":")
        if mid in self.machines:
            m = self.machines[mid]
            if port == "in":
                return m.inputs
            if port == "out":
                return m.outputs
        raise KeyError(f"unknown belt endpoint {ref!r} ({direction})")

    # ---- power
    def _all_circuits(self) -> set[int]:
        cs = {m.circuit for m in self.machines.values()} | {g.circuit for g in self.generators.values()}
        for s in self.switches.values():
            cs |= {s.a, s.b}
        return cs

    def _recompute_grids(self) -> None:
        parent: dict[int, int] = {c: c for c in self._all_circuits()}

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for s in self.switches.values():
            if s.on:
                ra, rb = find(s.a), find(s.b)
                if ra != rb:
                    parent[max(ra, rb)] = min(ra, rb)
        groups: dict[int, set[int]] = {}
        for c in parent:
            groups.setdefault(find(c), set()).add(c)
        grids = []
        for _, cs in sorted(groups.items()):
            cap = sum(g.capacity_mw for g in self.generators.values() if g.circuit in cs and g.online)
            ms = [m for m in self.machines.values() if m.circuit in cs]
            demand = sum(m.power_mw for m in ms if m.wants_power())
            maxc = sum(m.power_mw for m in ms)
            grids.append(Grid(min(cs), frozenset(cs), cap, demand, maxc, bool(cs & self.fused)))
        self.grids = grids

    def grid_of(self, circuit: int) -> Grid:
        for g in self.grids:
            if circuit in g.circuits:
                return g
        raise KeyError(circuit)

    def _resolve_power(self) -> None:
        for _ in range(32):
            self._recompute_grids()
            # a grid without any generation is simply unpowered; fuses only trip on overload
            over = next((g for g in self.grids if not g.fuse and g.capacity > EPS and g.demand > g.capacity + EPS), None)
            if over is None:
                return
            sheddable = [s for s in self.switches.values() if s.on and s.priority > 0 and (s.a in over.circuits or s.b in over.circuits)]
            if sheddable:
                victim = max(sheddable, key=lambda s: (s.priority, s.id))
                victim.on = False
                self._event("priority_shed", switch=victim.id, grid=over.id)
                continue
            self.fused |= set(over.circuits)
            self._event("fuse_tripped", grid=over.id, demand=over.demand, capacity=over.capacity)
        self._recompute_grids()

    # ---- simulation step
    def step(self, dt: float) -> None:
        if self.game_paused or dt <= 0:
            return
        for s in self.supplies.values():
            if not s.enabled:
                continue
            s.accum = min(s.accum + s.rate_per_min / 60.0 * dt, 1e9)
            n = int(s.accum + EPS)
            if s.remaining is not None:
                n = min(n, s.remaining)
            moved = self._inv(s.dst, "dst").add(s.item, n)
            s.accum -= moved
            if s.remaining is not None:
                s.remaining -= moved
            s.accum = min(s.accum, 1.0)  # a blocked line does not bank items
        for b in self.belts:
            if not b.enabled:
                continue
            src, dst = self._inv(b.src, "src"), self._inv(b.dst, "dst")
            b.accum = min(b.accum + b.rate_per_min / 60.0 * dt, max(1.0, b.rate_per_min / 60.0 * dt))
            n = min(int(b.accum + EPS), src.count(b.item), dst.room(b.item))
            if n > 0:
                src.take(b.item, n)
                dst.add(b.item, n)
                b.accum -= n
                b.moved += n
            else:
                b.accum = min(b.accum, 1.0)
        self._resolve_power()
        powered_circuits: set[int] = set()
        for g in self.grids:
            if not g.fuse and g.capacity > EPS:
                powered_circuits |= set(g.circuits)
        for m in self.machines.values():
            m.step(dt, m.circuit in powered_circuits)
        self._burn_fuel(dt)
        self.game_time += dt
        self._record_transitions()

    def _burn_fuel(self, dt: float) -> None:
        """Fuel-limited generators burn while their grid draws power; at 0 they go offline."""
        for g in self.generators.values():
            if g.fuel_s is None or not g.online:
                continue
            grid = self.grid_of(g.circuit)
            draw = sum(m.power_mw for m in self.machines.values() if m.circuit in grid.circuits and m.producing)
            if draw <= EPS or grid.fuse:
                continue
            burn = min(dt, g.fuel_s)
            g.fuel_s -= burn
            g.fuel_used_s += burn
            if g.fuel_s <= EPS:
                g.fuel_s = 0.0
                g.online = False
                self._event("generator_out_of_fuel", id=g.id)
                self._recompute_grids()

    def _record_transitions(self) -> None:
        for m in self.machines.values():
            st = (m.paused, m.producing, m.status)
            if self._last_state.get(m.id) != st:
                self._last_state[m.id] = st
                self.timeline.append(
                    {"t": round(self.game_time, 3), "id": m.id, "paused": m.paused, "producing": m.producing, "status": m.status}
                )

    def _event(self, kind: str, **data: Any) -> None:
        self.events.append({"t": round(self.game_time, 3), "kind": kind, **data})

    # ---- controls (used by FRM writes and by simulated humans)
    def set_paused(self, mid: str, paused: bool, by: str = "frm") -> None:
        m = self.machines[mid]
        if m.paused != paused:
            m.paused = paused
            self._event("set_paused", id=mid, paused=paused, by=by)
            self._recompute_grids()
            self._record_transitions()

    def set_switch(self, sid: str, on: bool | None = None, priority: int | None = None, name: str | None = None, by: str = "frm") -> None:
        s = self.switches[sid]
        if on is not None and s.on != on:
            s.on = on
            self._event("set_switch", id=sid, on=on, by=by)
        if priority is not None and s.priority > 0:  # only priority switches have a priority
            new = max(1, min(8, int(priority)))
            if new != s.priority:
                s.priority = new
                self._event("set_priority", id=sid, priority=new, by=by)
        if name is not None:
            s.name = name
        self._recompute_grids()

    def reset_fuse(self, circuit: int | None = None, by: str = "human") -> None:
        if circuit is None:
            self.fused.clear()
        else:
            self.fused -= set(self.grid_of(circuit).circuits)
        self._event("fuse_reset", circuit=circuit, by=by)
        self._recompute_grids()

    def add_chat(self, sender: str, message: str, kind: str = "Player") -> None:
        self.chat.append(
            {
                "TimeStamp": round(self.game_time, 3),
                "ServerTimeStamp": round(self.game_time, 3),
                "Sender": sender,
                "Type": kind,
                "Message": message,
                "Color": {"R": 1.0, "G": 1.0, "B": 1.0, "A": 1.0},
            }
        )

    def reload(self, rename_ids: bool = False) -> dict[str, str]:
        """Simulate save/load. Optionally gives buildings new runtime IDs (FRM IDs are not
        guaranteed stable across loads; open question L7). Returns old->new map."""
        mapping: dict[str, str] = {}
        if rename_ids:
            def bump(old: str) -> str:
                prefix, _, num = old.rpartition("_")
                new = f"{prefix}_{int(num) + 1000}" if num.isdigit() else old + "_r"
                mapping[old] = new
                return new

            self.machines = {bump(k): v for k, v in self.machines.items()}
            for k, m in self.machines.items():
                m.id = k
            self.storages = {bump(k): v for k, v in self.storages.items()}
            for k, s in self.storages.items():
                s.id = k

            def remap(ref: str) -> str:
                base, sep, port = ref.rpartition(":")
                if sep and base in mapping:
                    return f"{mapping[base]}:{port}"
                return mapping.get(ref, ref)

            for b in self.belts:
                b.src, b.dst = remap(b.src), remap(b.dst)
            for s in self.supplies.values():
                s.dst = remap(s.dst)
            self._last_state = {mapping.get(k, k): v for k, v in self._last_state.items()}
        self._event("reload", renamed=bool(mapping))
        return mapping

    # ---- truth helpers
    def running_ids(self) -> set[str]:
        return {m.id for m in self.machines.values() if not m.paused}

    def storage_total(self, cls: str) -> int:
        return sum(s.inventory.count(cls) for s in self.storages.values())


# --------------------------------------------------------------------------- scenario loading
def _loc(v: Any) -> tuple[float, float, float]:
    if not v:
        return (0.0, 0.0, 0.0)
    x, y, z = (list(v) + [0, 0, 0])[:3]
    return (float(x), float(y), float(z))


def build_world(scn: dict[str, Any]) -> World:
    """Build a World from a scenario mapping (see scenarios/*.toml)."""
    w = World(scn.get("session_name", "JawnSim"))
    for g in scn.get("generators", []):
        w.generators[g["id"]] = Generator(g["id"], g.get("name", "Generator"), int(g["circuit"]), float(g["capacity_mw"]), bool(g.get("online", True)), g.get("class_name", "Build_GeneratorCoal_C"), float(g["fuel_s"]) if g.get("fuel_s") is not None else None)
    for s in scn.get("switches", []):
        w.switches[s["id"]] = PowerSwitch(s["id"], s.get("name", s["id"]), int(s["a"]), int(s["b"]), bool(s.get("on", True)), int(s.get("priority", 0)), s.get("class_name", "Build_PowerSwitch_C"))
    for st in scn.get("storages", []):
        box = StorageBox(st["id"], st.get("name", "Storage Container"), st.get("class_name", "Build_StorageContainerMk1_C"), _loc(st.get("location")), SlotInventory(int(st.get("slots", 24))))
        for cls, n in st.get("items", {}).items():
            added = box.inventory.add(cls, int(n))
            if added != int(n):
                raise ValueError(f"storage {st['id']} cannot hold {n} x {cls}")
        w.add_storage(box)
    for m in scn.get("machines", []):
        recipe = RECIPES[m["recipe"]] if m.get("recipe") else None
        cls = m["class_name"]
        bdef = BUILDINGS.get(cls)
        mach = Machine(
            id=m["id"],
            name=m.get("name", bdef.name if bdef else cls),
            class_name=cls,
            recipe=recipe,
            circuit=int(m.get("circuit", 1)),
            location=_loc(m.get("location")),
            paused=bool(m.get("paused", False)),
            clock=float(m.get("clock", 1.0)),
            power_mw=float(m.get("power_mw", bdef.power_mw if bdef else 4.0)),
        )
        for cls_in, n in m.get("input_items", {}).items():
            mach.inputs.add(cls_in, int(n))
        w.add_machine(mach)
    for b in scn.get("belts", []):
        w.belts.append(Belt(b["id"], b["src"], b["dst"], b["item"], float(b.get("rate_per_min", 60.0)), bool(b.get("enabled", True))))
    for s in scn.get("supplies", []):
        w.supplies[s["id"]] = Supply(s["id"], s["item"], s["dst"], float(s["rate_per_min"]), bool(s.get("enabled", True)), s.get("remaining"))
    for ref in [b.src for b in w.belts] + [b.dst for b in w.belts] + [s.dst for s in w.supplies.values()]:
        w._inv(ref, "check")
    w._recompute_grids()
    w._record_transitions()
    return w


__all__ = ["World", "build_world", "Machine", "StorageBox", "Belt", "Supply", "Generator", "PowerSwitch", "ITEMS"]
