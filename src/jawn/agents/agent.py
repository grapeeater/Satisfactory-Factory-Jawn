"""Independent agent runtime: own objective, view, memory, decision history, inbox, triggers.

Independence guarantees (tested):
  * an agent's prompt contains only its own memory/history/results and messages
    explicitly addressed to it; never another agent's reasoning or memory
  * agents act only on machines they own (validator enforces authority)
  * agents are woken individually by their own triggers
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from ..config import AgentConfig
from ..context import TickContext, item_name
from ..observe.snapshot import WRITE_SAFE


@dataclass
class AgentRuntime:
    cfg: AgentConfig
    peers: list[str]
    memory: str = ""
    history: deque[dict[str, Any]] = field(default_factory=deque)
    inbox: list[dict[str, Any]] = field(default_factory=list)
    results: list[dict[str, Any]] = field(default_factory=list)
    last_call_at: float | None = None
    last_sig: tuple[Any, ...] | None = None
    calls: int = 0
    invalid: int = 0
    decisions: int = 0
    task: asyncio.Task[Any] | None = None
    call_started: float = 0.0
    call_meta: dict[str, Any] = field(default_factory=dict)
    exhausted: str | None = None
    last_wake_reasons: list[str] = field(default_factory=list)
    deferred: int = 0

    def __post_init__(self) -> None:
        self.history = deque(maxlen=self.cfg.history_len)

    @property
    def id(self) -> str:
        return self.cfg.id

    @property
    def busy(self) -> bool:
        return self.task is not None

    # ------------------------------------------------------------------ view
    def build_view(self, ctx: TickContext, owners: dict[str, str]) -> dict[str, Any]:
        st, snap = ctx.st, ctx.snap
        mine = []
        for alias in sorted(self.cfg.owns):
            m = ctx.machine(alias)
            lk = st.locked(alias, ctx.now)
            if m is None:
                mine.append({"alias": alias, "state": "missing"})
                continue
            prod_item = ctx.cfg.targets[alias].product
            mine.append(
                {
                    "alias": alias,
                    "state": m.condition(),
                    "recipe": m.recipe,
                    "input_buffer": {item_name(c): n for c, n in sorted(m.inputs.items())},
                    "output_buffer": {item_name(c): n for c, n in sorted(m.outputs.items())},
                    "max_rate_per_min": round(m.max_prod.get(prod_item or "", 0.0), 2),
                    "pending_command": alias in st.pending,
                    "human_lock_s": round(lk.until - ctx.now) if lk else 0,
                    "upstream_stock": {item_name(c): n for c, n in sorted(ctx.upstream(alias).items())},
                }
            )
        progress = []
        for it, n in sorted(self.cfg.targets.items()):
            got = ctx.outcome.total(it)
            progress.append({"item": item_name(it), "delivered": got, "target": n, "remaining": max(0, n - got)})
        peers = []
        for alias, owner in sorted(owners.items()):
            if owner == self.id:
                continue
            m = ctx.machine(alias)
            peers.append({"alias": alias, "owner": owner, "state": m.condition() if m else "missing"})
        groups = []
        for c in ctx.cfg.constraints:
            if set(c.targets) & set(self.cfg.owns):
                running = sorted(a for a in c.targets if st.running(snap, a, ctx.cfg.safety.count_pending_as_running))
                groups.append({"name": c.name, "max_running": c.max, "members": sorted(c.targets), "running": running})
        circuits = set()
        for alias in self.cfg.owns:
            m = ctx.machine(alias)
            if m and m.circuit_id is not None:
                circuits.add(m.circuit_id)
        power = [
            {"circuit": cid, "capacity_mw": round(snap.circuits[cid].capacity, 1), "used_mw": round(snap.circuits[cid].consumed, 1), "fuse_tripped": snap.circuits[cid].fuse}
            for cid in sorted(circuits)
            if cid in snap.circuits
        ]
        return {
            "time": {"elapsed_s": int(ctx.elapsed), "remaining_s": int(max(0, ctx.time_left))},
            "observation": {"seq": snap.seq, "health": snap.health.value},
            "progress": progress,
            "your_machines": mine,
            "other_machines": peers,
            "constraints": groups,
            "power": power,
            "results_since_last_decision": list(self.results),
            "messages_to_you": list(self.inbox),
            "your_memory": self.memory,
            "your_recent_decisions": list(self.history),
        }

    def signature(self, view: dict[str, Any]) -> tuple[Any, ...]:
        """Salient, discrete features. Counts and rates are excluded on purpose so steady
        production does not wake the agent (saves model calls)."""
        mine = tuple((m["alias"], m["state"], m.get("pending_command"), bool(m.get("human_lock_s")), tuple(v == 0 for v in m.get("upstream_stock", {}).values())) for m in view["your_machines"])
        prog = tuple((p["item"], p["remaining"] == 0) for p in view["progress"])
        peers = tuple((p["alias"], p["state"] in ("paused", "missing")) for p in view["other_machines"])
        groups = tuple((g["name"], tuple(g["running"])) for g in view["constraints"])
        power = tuple((p["circuit"], p["fuse_tripped"]) for p in view["power"])
        return (mine, prog, peers, groups, power, view["observation"]["health"])

    def done_and_idle(self, view: dict[str, Any]) -> bool:
        return bool(view["progress"]) and all(p["remaining"] == 0 for p in view["progress"]) and all(m["state"] in ("paused", "missing") for m in view["your_machines"])

    # ------------------------------------------------------------------ triggers
    def wake_reasons(self, ctx: TickContext, view: dict[str, Any]) -> list[str]:
        if self.busy or self.exhausted:
            return []
        if ctx.snap.health not in WRITE_SAFE:
            return []  # never pay for decisions on untrustworthy observations
        reasons = []
        sig = self.signature(view)
        if self.last_call_at is None:
            reasons.append("first_observation")
        else:
            if sig != self.last_sig:
                reasons.append("state_changed")
            if self.inbox:
                reasons.append("message")
            if self.results:
                reasons.append("results")
            if ctx.now - self.last_call_at >= self.cfg.triggers.heartbeat_s:
                reasons.append("heartbeat")
        if not reasons:
            return []
        if self.done_and_idle(view) and not self.inbox and self.last_call_at is not None:
            return []  # objective met and machines parked: nothing to decide
        if self.last_call_at is not None and ctx.now - self.last_call_at < self.cfg.triggers.min_interval_s:
            self.deferred += 1
            return []
        return reasons
