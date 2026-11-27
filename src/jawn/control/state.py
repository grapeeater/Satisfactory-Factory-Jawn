"""Shared control-plane state: alias bindings, pending commands, expectations, locks."""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from ..config import TargetBinding
from ..observe.snapshot import Snapshot
from .actions import ActionKind, Proposal


@dataclass
class Command:
    id: str
    proposal: Proposal
    alias: str
    target_id: str
    endpoint: str
    body: dict[str, Any]
    attr: str
    value: Any
    sent_at: float
    deadline: float
    status: str = "pending"  # pending -> sent -> verified | unverified | error | cancelled
    response: Any = None
    error: str | None = None
    verified_at: float | None = None
    after_seq: int = -1  # only observations with seq > after_seq can verify this command

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "proposal_id": self.proposal.id,
            "decision_id": self.proposal.decision_id,
            "source": self.proposal.source,
            "alias": self.alias,
            "target_id": self.target_id,
            "endpoint": self.endpoint,
            "body": self.body,
            "expect": {self.attr: self.value},
            "sent_at": round(self.sent_at, 3),
            "deadline": round(self.deadline, 3),
            "status": self.status,
            "error": self.error,
        }


@dataclass
class Lock:
    until: float
    reason: str
    by: str = "human"


@dataclass
class ControlState:
    bindings: dict[str, TargetBinding]
    resolved: dict[str, str] = field(default_factory=dict)  # alias -> current object ID
    missing: set[str] = field(default_factory=set)
    pending: dict[str, Command] = field(default_factory=dict)  # alias -> in-flight command
    expected: dict[str, Any] = field(default_factory=dict)  # alias -> state we believe/expect
    last_change: dict[str, float] = field(default_factory=dict)  # alias -> time of last harness-caused change
    locks: dict[str, Lock] = field(default_factory=dict)
    suspect: dict[str, float] = field(default_factory=dict)  # alias -> until (failed verification)
    initial: dict[str, Any] = field(default_factory=dict)  # alias -> state when harness took over
    harness_set: dict[str, bool] = field(default_factory=dict)  # alias -> current state was set by harness
    writes: deque[float] = field(default_factory=lambda: deque(maxlen=256))
    topology: dict[str, Any] = field(default_factory=dict)  # switch alias -> power.SwitchTopology (learned while open)
    initial_priority: dict[str, int | None] = field(default_factory=dict)  # switch alias -> priority at takeover
    priority_set: set[str] = field(default_factory=set)  # switches whose priority the harness changed
    stopped: bool = False
    held: bool = False  # soft pause: observe but do not write (chat "!jawn pause")
    history: list[Command] = field(default_factory=list)

    def __post_init__(self) -> None:
        for alias, b in self.bindings.items():
            self.resolved.setdefault(alias, b.id)

    # ---- binding resolution (open question L7: IDs may change across reloads)
    def resolve(self, snap: Snapshot) -> list[dict[str, Any]]:
        """Returns rebind/missing events. A missing target is never guessed at unless exactly
        one object matches class + recipe + location (within 1 m)."""
        events: list[dict[str, Any]] = []
        for alias, b in self.bindings.items():
            cur = self.resolved.get(alias, b.id)
            pool = snap.machines if b.kind == "machine" else snap.switches
            if cur in pool:
                if alias in self.missing:
                    self.missing.discard(alias)
                    events.append({"type": "target_found", "alias": alias, "id": cur})
                continue
            if b.kind == "machine" and "machines" not in snap.fresh:
                continue  # section failed this poll; carried data may be old, do not rebind
            cands = []
            if b.kind == "machine" and b.class_name:
                for m in snap.machines.values():
                    if m.class_name != b.class_name:
                        continue
                    if b.recipe_class and m.recipe_class != b.recipe_class:
                        continue
                    if b.location and m.location and math.dist(b.location, m.location) > 100.0:
                        continue
                    if not b.location:
                        continue
                    if m.id in self.resolved.values():
                        continue
                    cands.append(m.id)
            if len(cands) == 1:
                self.resolved[alias] = cands[0]
                self.missing.discard(alias)
                events.append({"type": "target_rebound", "alias": alias, "old_id": cur, "new_id": cands[0]})
            elif alias not in self.missing:
                self.missing.add(alias)
                events.append({"type": "target_missing", "alias": alias, "id": cur, "candidates": cands})
        return events

    # ---- observation helpers
    def observe(self, snap: Snapshot, alias: str) -> Any:
        b = self.bindings.get(alias)
        if b is None or alias in self.missing:
            return None
        oid = self.resolved.get(alias, b.id)
        if b.kind == "machine":
            m = snap.machines.get(oid)
            return None if m is None else m.paused
        s = snap.switches.get(oid)
        return None if s is None else s.on

    def observe_attr(self, snap: Snapshot, alias: str, attr: str) -> Any:
        """Observed value of a specific attribute ('paused', 'on', 'priority')."""
        if attr in ("paused", "on"):
            return self.observe(snap, alias)
        b = self.bindings.get(alias)
        if b is None or alias in self.missing or b.kind != "switch":
            return None
        s = snap.switches.get(self.resolved.get(alias, b.id))
        return None if s is None else getattr(s, attr, None)

    @staticmethod
    def attr_of(kind: ActionKind) -> str:
        if kind == ActionKind.SWITCH_SET_PRIORITY:
            return "priority"
        return "on" if kind in (ActionKind.SWITCH_ON, ActionKind.SWITCH_OFF) else "paused"

    def locked(self, alias: str, now: float) -> Lock | None:
        lk = self.locks.get(alias)
        if lk and lk.until > now:
            return lk
        if lk:
            del self.locks[alias]
        return None

    def is_suspect(self, alias: str, now: float) -> bool:
        until = self.suspect.get(alias)
        if until is None:
            return False
        if until <= now:
            del self.suspect[alias]
            return False
        return True

    def writes_in_last(self, window: float, now: float) -> int:
        return sum(1 for t in self.writes if now - t <= window)

    def running(self, snap: Snapshot, alias: str, count_pending: bool) -> bool:
        """Whether a machine counts as 'running' (enabled) for constraint purposes.
        Fail-safe: unknown state (missing from observations) counts as running; a pending
        enable counts as running; with count_pending, a pending disable is still running
        until verified."""
        obs = self.observe(snap, alias)
        if obs is None:
            return True
        p = self.pending.get(alias)
        if p is not None and p.attr == "paused":
            if p.value is False:
                return True
            if count_pending:
                return obs is False
        return obs is False
