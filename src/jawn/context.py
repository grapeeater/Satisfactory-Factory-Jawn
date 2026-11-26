"""Per-tick context shared by controllers, plus small helpers about targets and progress."""

from __future__ import annotations

from dataclasses import dataclass

from .config import Config
from .control.state import ControlState
from .observe.outcome import OutcomeMeter
from .observe.snapshot import MachineObs, Snapshot
from .sim.catalog import ITEMS, RECIPES


@dataclass
class TickContext:
    now: float
    snap: Snapshot
    st: ControlState
    cfg: Config
    outcome: OutcomeMeter
    elapsed: float
    time_left: float

    def machine(self, alias: str) -> MachineObs | None:
        oid = self.st.resolved.get(alias)
        if oid is None or alias in self.st.missing:
            return None
        return self.snap.machines.get(oid)

    def target_for(self, alias: str) -> tuple[str | None, int | None]:
        b = self.cfg.targets.get(alias)
        if b is None or not b.product:
            return None, None
        owner = self.cfg.owner_of(alias)
        for a in self.cfg.agents:
            if a.id == owner:
                return b.product, a.targets.get(b.product)
        return b.product, None

    def remaining_fraction(self, alias: str) -> float:
        item, target = self.target_for(alias)
        if not item or not target:
            return 0.0
        return max(0.0, (target - self.outcome.total(item)) / target)

    def done(self, alias: str) -> bool:
        item, target = self.target_for(alias)
        return bool(item and target and self.outcome.total(item) >= target)

    def upstream(self, alias: str) -> dict[str, int]:
        m = self.machine(alias)
        if m is None:
            return {}
        rec = RECIPES.get(m.recipe_class)
        inputs = list(rec.inputs) if rec else list(m.inputs)
        return {c: self.snap.storage_total(c) for c in inputs}

    def can_work(self, alias: str) -> bool:
        """Inputs buffered for a cycle, or available upstream."""
        m = self.machine(alias)
        if m is None:
            return False
        if not m.lacks_inputs():
            return True
        rec = RECIPES.get(m.recipe_class)
        up = self.upstream(alias)
        if rec:
            return all(m.inputs.get(c, 0) + up.get(c, 0) >= n for c, n in rec.inputs.items())
        return any(v > 0 for v in up.values())


def item_name(cls: str) -> str:
    it = ITEMS.get(cls)
    return it.name if it else cls


def priority_for(ctx: TickContext, alias: str, weight: float = 1.0) -> float:
    return round(weight * ctx.remaining_fraction(alias), 6)
