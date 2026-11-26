"""Action vocabulary, proposals and backend capability registry.

Extensibility: new management capabilities (recipes, clocks, logistics) are added by
declaring an ActionKind, the state it targets, and a backend mapping. Kinds that no
configured backend supports are rejected by the validator with UNSUPPORTED_CAPABILITY,
so agents can be offered them only once a backend actually implements them.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ActionKind(str, Enum):
    WAIT = "wait"
    MACHINE_ENABLE = "machine.enable"
    MACHINE_DISABLE = "machine.disable"
    SWITCH_ON = "switch.on"
    SWITCH_OFF = "switch.off"
    # Declared for future backends; FRM 1.5.6 has no endpoint for these.
    MACHINE_SET_RECIPE = "machine.set_recipe"
    MACHINE_SET_CLOCK = "machine.set_clock"
    SWITCH_SET_PRIORITY = "switch.set_priority"


@dataclass(frozen=True)
class ActionSpec:
    kind: ActionKind
    target_kind: str  # "machine" | "switch" | "none"
    # desired observable state after success: (attribute on observation, value)
    expect: tuple[str, Any] | None
    increases_load: bool = False


SPECS: dict[ActionKind, ActionSpec] = {
    ActionKind.WAIT: ActionSpec(ActionKind.WAIT, "none", None),
    ActionKind.MACHINE_ENABLE: ActionSpec(ActionKind.MACHINE_ENABLE, "machine", ("paused", False), increases_load=True),
    ActionKind.MACHINE_DISABLE: ActionSpec(ActionKind.MACHINE_DISABLE, "machine", ("paused", True)),
    ActionKind.SWITCH_ON: ActionSpec(ActionKind.SWITCH_ON, "switch", ("on", True), increases_load=True),
    ActionKind.SWITCH_OFF: ActionSpec(ActionKind.SWITCH_OFF, "switch", ("on", False)),
    ActionKind.MACHINE_SET_RECIPE: ActionSpec(ActionKind.MACHINE_SET_RECIPE, "machine", None),
    ActionKind.MACHINE_SET_CLOCK: ActionSpec(ActionKind.MACHINE_SET_CLOCK, "machine", None),
    ActionKind.SWITCH_SET_PRIORITY: ActionSpec(ActionKind.SWITCH_SET_PRIORITY, "switch", None),
}

# What each backend can actually do. FRM 1.5.6: setEnabled (machines, switches), setSwitches.
FRM_CAPABILITIES: frozenset[ActionKind] = frozenset(
    {ActionKind.WAIT, ActionKind.MACHINE_ENABLE, ActionKind.MACHINE_DISABLE, ActionKind.SWITCH_ON, ActionKind.SWITCH_OFF}
)

# simple short-form names accepted from model output
SHORT_NAMES = {"enable": ActionKind.MACHINE_ENABLE, "disable": ActionKind.MACHINE_DISABLE, "wait": ActionKind.WAIT, "switch_on": ActionKind.SWITCH_ON, "switch_off": ActionKind.SWITCH_OFF}

_ids = itertools.count(1)


def new_id(prefix: str) -> str:
    return f"{prefix}-{next(_ids):05d}"


def reset_ids() -> None:
    global _ids
    _ids = itertools.count(1)


@dataclass
class Proposal:
    source: str  # agent id | "baseline" | "harness" (setup/stop) | "human"
    kind: ActionKind
    target: str  # alias ("" for wait)
    reason: str
    based_on_seq: int
    based_on_time: float
    created_at: float
    observed_state: Any = None  # target state the proposer saw (for stale-state checks)
    priority: float = 0.0  # arbitration priority (higher first)
    decision_id: str | None = None
    id: str = field(default_factory=lambda: new_id("prop"))
    params: dict[str, Any] = field(default_factory=dict)
    system: bool = False  # harness setup/stop actions (bypass agent-only rules, never safety rules)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "decision_id": self.decision_id,
            "source": self.source,
            "kind": self.kind.value,
            "target": self.target,
            "reason": self.reason,
            "based_on_seq": self.based_on_seq,
            "based_on_time": round(self.based_on_time, 3),
            "observed_state": self.observed_state,
            "priority": round(self.priority, 4),
            "system": self.system,
        }
