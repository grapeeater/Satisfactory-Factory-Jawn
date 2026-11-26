"""The single reader of FRM. Agents never call FRM; they read snapshots produced here."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable

from ..clock import Clock
from ..config import SafetyConfig
from ..frm.client import FrmClient, FrmError
from .snapshot import (
    Health,
    Snapshot,
    parse_chat,
    parse_machines,
    parse_power,
    parse_session,
    parse_storages,
    parse_switches,
)

MACHINE_ENDPOINTS = {"getFactory", "getConstructor", "getAssembler", "getSmelter", "getManufacturer", "getRefinery", "getFoundry", "getPackager", "getBlender"}
SECTION_OF = {"getStorageInv": "storages", "getPower": "power", "getSwitches": "switches", "getSessionInfo": "session", "getChatMessages": "chat"}


def section_of(endpoint: str) -> str | None:
    if endpoint in MACHINE_ENDPOINTS:
        return "machines"
    return SECTION_OF.get(endpoint)


@dataclass
class _Section:
    data: Any = None
    fresh_at: float | None = None


@dataclass
class Observer:
    client: FrmClient
    clock: Clock
    endpoints: list[str]
    safety: SafetyConfig
    seq: int = 0
    _sections: dict[str, _Section] = field(default_factory=dict)
    _last_game_time: float | None = None
    _game_time_advanced_at: float | None = None
    on_snapshot: list[Callable[[Snapshot], None]] = field(default_factory=list)
    last: Snapshot | None = None

    def __post_init__(self) -> None:
        unknown = [e for e in self.endpoints if section_of(e) is None]
        if unknown:
            raise ValueError(f"observer cannot use endpoints {unknown}")
        if not any(section_of(e) == "machines" for e in self.endpoints):
            raise ValueError("observer needs at least one machine endpoint (e.g. getFactory)")

    async def poll(self) -> Snapshot:
        started = self.clock.now()
        results = await asyncio.gather(*(self._get(e) for e in self.endpoints))
        now = self.clock.now()
        errors: dict[str, str] = {}
        error_kinds: list[str] = []
        fresh_machine_parts: list[Any] = []
        fresh: set[str] = set()
        for ep, (ok, val) in zip(self.endpoints, results):
            sec = section_of(ep)
            assert sec is not None
            if not ok:
                errors[ep] = str(val)
                error_kinds.append(val.kind if isinstance(val, FrmError) else "error")
                continue
            fresh.add(sec)
            if sec == "machines":
                fresh_machine_parts.append(val)
            else:
                self._sections[sec] = _Section(val, started)
        if fresh_machine_parts:
            merged: list[Any] = []
            for part in fresh_machine_parts:
                if isinstance(part, list):
                    merged.extend(part)
            # de-duplicate machines that appear in several endpoints (getFactory + getConstructor)
            seen: dict[str, Any] = {}
            for d in merged:
                if isinstance(d, dict) and d.get("ID"):
                    seen[str(d["ID"])] = d
            self._sections["machines"] = _Section(list(seen.values()), started)

        self.seq += 1
        perr: list[str] = []
        sec = self._sections
        snap = Snapshot(seq=self.seq, started_at=started, taken_at=now, game_time=None, game_paused_flag=None, errors=errors)
        if "machines" in sec:
            snap.machines = parse_machines(sec["machines"].data, perr)
        if "storages" in sec:
            snap.storages = parse_storages(sec["storages"].data, perr)
        if "power" in sec:
            snap.circuits = parse_power(sec["power"].data, perr)
        if "switches" in sec:
            snap.switches = parse_switches(sec["switches"].data, perr)
        if "session" in sec:
            snap.game_time, snap.game_paused_flag = parse_session(sec["session"].data, perr)
        if "chat" in fresh:
            snap.chat = parse_chat(sec["chat"].data, perr)
        snap.parse_errors = perr
        snap.section_age = {k: round(now - v.fresh_at, 3) for k, v in sec.items() if v.fresh_at is not None}
        snap.fresh = fresh
        snap.health = self._health(snap, error_kinds, now)
        self.last = snap
        for cb in self.on_snapshot:
            cb(snap)
        return snap

    async def _get(self, endpoint: str) -> tuple[bool, Any]:
        try:
            return True, await self.client.get(endpoint)
        except FrmError as e:
            return False, e

    def _health(self, snap: Snapshot, error_kinds: list[str], now: float) -> Health:
        n = len(self.endpoints)
        if error_kinds and len(error_kinds) == n:
            if all(k == "not_found" for k in error_kinds):
                return Health.NOT_FOUND
            if "machines" not in self._sections:
                return Health.DOWN
        m_age = snap.section_age.get("machines")
        if m_age is None:
            return Health.DOWN
        if m_age > self.safety.max_snapshot_age_s:
            return Health.STALE
        # game clock progress (only judged on fresh session data)
        if "session" in snap.fresh and snap.game_time is not None:
            if self._last_game_time is None or snap.game_time > self._last_game_time + 1e-9:
                self._game_time_advanced_at = now
            self._last_game_time = snap.game_time
            if snap.game_paused_flag:
                return Health.GAME_PAUSED
            # game clock stuck => paused menu or frozen/stale FRM data; either way, do not act.
            # (Per-machine "frozen" heuristics are unreliable: steady-state machines legitimately
            # report identical values poll after poll.)
            if self._game_time_advanced_at is not None and now - self._game_time_advanced_at > self.safety.game_paused_after_s:
                return Health.GAME_PAUSED
        elif "session" in self.endpoints_sections() and snap.section_age.get("session", 0) > self.safety.max_snapshot_age_s:
            return Health.STALE
        if error_kinds or snap.parse_errors:
            return Health.DEGRADED
        return Health.OK

    def endpoints_sections(self) -> set[str]:
        return {s for s in (section_of(e) for e in self.endpoints) if s}
