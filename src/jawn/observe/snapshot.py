"""Normalized game-state observations and tolerant FRM parsing.

Parsing never raises on unexpected shapes: problems are recorded in ``Snapshot.parse_errors``
so a single odd object cannot take the observer down, and missing data is distinguishable
from "false"/"zero" (fields are Optional where FRM might omit them).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..sim.catalog import RECIPES  # static game data (recipes), not simulator state


class Health(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"  # some sections failed this poll; their data is carried over and aged
    STALE = "stale"  # no fresh machine data within max_snapshot_age_s
    GAME_PAUSED = "game_paused"  # game time not advancing (pause menu, or frozen data)
    NOT_FOUND = "not_found"  # every endpoint 404 (FRM issue #313 after reload) -> restart game
    DOWN = "down"  # nothing reachable


WRITE_SAFE = {Health.OK, Health.DEGRADED}


@dataclass
class MachineObs:
    id: str
    name: str
    class_name: str
    recipe: str
    recipe_class: str
    paused: bool | None
    producing: bool | None
    configured: bool | None
    circuit_id: int | None
    power_mw: float
    max_power_mw: float
    productivity: float | None
    clock_pct: float | None
    inputs: dict[str, int] = field(default_factory=dict)
    input_caps: dict[str, int] = field(default_factory=dict)
    outputs: dict[str, int] = field(default_factory=dict)
    output_caps: dict[str, int] = field(default_factory=dict)
    max_prod: dict[str, float] = field(default_factory=dict)
    current_prod: dict[str, float] = field(default_factory=dict)
    location: tuple[float, float, float] | None = None

    def lacks_inputs(self) -> bool:
        """True when buffered inputs cannot start a cycle (uses recipe data when known)."""
        rec = RECIPES.get(self.recipe_class)
        if rec is not None:
            return any(self.inputs.get(c, 0) < n for c, n in rec.inputs.items())
        return any(v == 0 for v in self.inputs.values())

    @property
    def starved(self) -> bool:
        return bool(self.paused is False and self.producing is False and self.lacks_inputs())

    @property
    def blocked(self) -> bool:
        return bool(self.paused is False and self.producing is False and any(self.outputs.get(k, 0) >= cap > 0 for k, cap in self.output_caps.items()))

    def condition(self) -> str:
        if self.paused:
            return "paused"
        if self.producing:
            return "producing"
        if self.blocked:
            return "output_full"
        if self.starved:
            return "starved"
        return "idle"


@dataclass
class StorageObs:
    id: str
    name: str
    items: dict[str, int]


@dataclass
class CircuitObs:
    id: int
    consumed: float
    capacity: float
    max_consumed: float
    fuse: bool

    @property
    def headroom(self) -> float:
        return self.capacity - self.consumed


@dataclass
class SwitchObs:
    id: str
    name: str
    on: bool | None
    priority: int | None
    circuits: tuple[int | None, int | None] = (None, None)


@dataclass
class ChatObs:
    timestamp: float
    sender: str
    kind: str
    message: str


@dataclass
class Snapshot:
    seq: int
    started_at: float  # harness clock when the poll began (anything sent before this is visible)
    taken_at: float  # harness clock when the poll completed
    game_time: float | None
    game_paused_flag: bool | None
    machines: dict[str, MachineObs] = field(default_factory=dict)
    storages: dict[str, StorageObs] = field(default_factory=dict)
    circuits: dict[int, CircuitObs] = field(default_factory=dict)
    switches: dict[str, SwitchObs] = field(default_factory=dict)
    chat: list[ChatObs] = field(default_factory=list)
    section_age: dict[str, float] = field(default_factory=dict)  # seconds since section was last fresh
    errors: dict[str, str] = field(default_factory=dict)  # endpoint -> error this poll
    parse_errors: list[str] = field(default_factory=list)
    fresh: set[str] = field(default_factory=set)  # sections successfully fetched in this poll
    health: Health = Health.OK

    def storage_total(self, cls: str) -> int:
        return sum(s.items.get(cls, 0) for s in self.storages.values())

    def summary(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "t": round(self.taken_at, 3),
            "game_time": self.game_time,
            "health": self.health.value,
            "machines": {m.id: m.condition() for m in self.machines.values()},
            "errors": self.errors,
        }


# --------------------------------------------------------------------------- parsing helpers
def _b(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        if v.lower() in ("true", "1", "yes"):
            return True
        if v.lower() in ("false", "0", "no"):
            return False
    return None


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _i(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _first(d: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if k in d:
            return d[k]
    return None


def _inv(entries: Any) -> tuple[dict[str, int], dict[str, int]]:
    amounts: dict[str, int] = {}
    caps: dict[str, int] = {}
    if isinstance(entries, list):
        for e in entries:
            if not isinstance(e, dict):
                continue
            cls = e.get("ClassName") or e.get("Name")
            if not cls:
                continue
            amounts[cls] = amounts.get(cls, 0) + int(_f(e.get("Amount")))
            if "MaxAmount" in e:
                caps[cls] = int(_f(e.get("MaxAmount")))
    return amounts, caps


def _loc(d: Any) -> tuple[float, float, float] | None:
    if isinstance(d, dict) and {"x", "y", "z"} <= set(d):
        return (_f(d["x"]), _f(d["y"]), _f(d["z"]))
    return None


def parse_machines(data: Any, errors: list[str]) -> dict[str, MachineObs]:
    out: dict[str, MachineObs] = {}
    if not isinstance(data, list):
        errors.append(f"machines: expected list, got {type(data).__name__}")
        return out
    for d in data:
        if not isinstance(d, dict) or not d.get("ID"):
            errors.append("machines: entry without ID skipped")
            continue
        try:
            pinfo = d.get("PowerInfo") if isinstance(d.get("PowerInfo"), dict) else {}
            inputs, in_caps = _inv(d.get("InputInventory"))
            outputs, out_caps = _inv(d.get("OutputInventory"))
            # fall back to production/ingredients "Amount" when inventories are absent
            for p in d.get("production") or []:
                if isinstance(p, dict) and p.get("ClassName") and p["ClassName"] not in outputs:
                    outputs[p["ClassName"]] = int(_f(p.get("Amount")))
            for p in d.get("ingredients") or []:
                if isinstance(p, dict) and p.get("ClassName") and p["ClassName"] not in inputs:
                    inputs[p["ClassName"]] = int(_f(p.get("Amount")))
            prod = d.get("production") or []
            m = MachineObs(
                id=str(d["ID"]),
                name=str(d.get("Name", "")),
                class_name=str(d.get("ClassName", "")),
                recipe=str(d.get("Recipe", "")),
                recipe_class=str(d.get("RecipeClassName", "")),
                paused=_b(d.get("IsPaused")),
                producing=_b(d.get("IsProducing")),
                configured=_b(d.get("IsConfigured")),
                circuit_id=_i(_first(pinfo, "CircuitGroupID", "CircuitID") if pinfo else d.get("CircuitGroupID")),
                power_mw=_f(pinfo.get("PowerConsumed")) if pinfo else 0.0,
                max_power_mw=_f(pinfo.get("MaxPowerConsumed")) if pinfo else 0.0,
                productivity=_f(d.get("Productivity")) if "Productivity" in d else None,
                clock_pct=_f(d.get("ManuSpeed")) if "ManuSpeed" in d else None,
                inputs=inputs,
                input_caps=in_caps,
                outputs=outputs,
                output_caps=out_caps,
                max_prod={p["ClassName"]: _f(p.get("MaxProd")) for p in prod if isinstance(p, dict) and p.get("ClassName")},
                current_prod={p["ClassName"]: _f(p.get("CurrentProd")) for p in prod if isinstance(p, dict) and p.get("ClassName")},
                location=_loc(d.get("location")),
            )
            out[m.id] = m
        except Exception as e:  # noqa: BLE001 - tolerate any odd entry
            errors.append(f"machines: {d.get('ID')}: {type(e).__name__}: {e}")
    return out


def parse_storages(data: Any, errors: list[str]) -> dict[str, StorageObs]:
    out: dict[str, StorageObs] = {}
    if not isinstance(data, list):
        errors.append("storages: expected list")
        return out
    for d in data:
        if isinstance(d, dict) and d.get("ID"):
            items, _ = _inv(d.get("Inventory"))
            out[str(d["ID"])] = StorageObs(str(d["ID"]), str(d.get("Name", "")), items)
    return out


def parse_power(data: Any, errors: list[str]) -> dict[int, CircuitObs]:
    out: dict[int, CircuitObs] = {}
    if not isinstance(data, list):
        errors.append("power: expected list")
        return out
    for d in data:
        if not isinstance(d, dict):
            continue
        cid = _i(_first(d, "CircuitGroupID", "CircuitID"))
        if cid is None:
            errors.append("power: circuit without id")
            continue
        out[cid] = CircuitObs(
            id=cid,
            consumed=_f(d.get("PowerConsumed")),
            capacity=_f(d.get("PowerCapacity")),
            max_consumed=_f(d.get("PowerMaxConsumed")),
            fuse=bool(_b(d.get("FuseTriggered"))),
        )
    return out


def parse_switches(data: Any, errors: list[str]) -> dict[str, SwitchObs]:
    out: dict[str, SwitchObs] = {}
    if not isinstance(data, list):
        errors.append("switches: expected list")
        return out
    for d in data:
        if isinstance(d, dict) and d.get("ID"):
            out[str(d["ID"])] = SwitchObs(
                str(d["ID"]), str(_first(d, "SwitchTag", "Name") or ""), _b(_first(d, "IsOn", "Status")), _i(d.get("Priority")), (_i(d.get("Connected0")), _i(d.get("Connected1")))
            )
    return out


def parse_session(data: Any, errors: list[str]) -> tuple[float | None, bool | None]:
    if not isinstance(data, dict):
        if isinstance(data, list) and data and isinstance(data[0], dict):
            data = data[0]
        else:
            errors.append("session: unexpected shape")
            return None, None
    gt = data.get("TotalPlayDuration")
    return (_f(gt) if gt is not None else None), _b(data.get("IsPaused"))


def parse_chat(data: Any, errors: list[str]) -> list[ChatObs]:
    out: list[ChatObs] = []
    if not isinstance(data, list):
        errors.append("chat: expected list")
        return out
    for d in data:
        if isinstance(d, dict) and "Message" in d:
            out.append(ChatObs(_f(_first(d, "ServerTimeStamp", "TimeStamp")), str(d.get("Sender", "")), str(d.get("Type", "")), str(d.get("Message", ""))))
    return out
