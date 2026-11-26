"""Single-writer executor and independent readback verifier.

A command's lifecycle is logged in three separate records:
  command_sent      what the harness asked FRM to do
  command_response  what FRM said (HTTP-level acceptance is NOT treated as success)
  verification      what a *later* observation shows (verified / unverified)
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from ..clock import Clock
from ..config import SafetyConfig
from ..frm.client import FrmClient, FrmError
from ..observe.snapshot import Snapshot
from .actions import SPECS, ActionKind, Proposal, new_id
from .state import Command, ControlState

Emit = Callable[..., None]


def backend_call(kind: ActionKind, target_id: str) -> tuple[str, dict[str, Any]]:
    """Map an action onto FRM 1.5.6 write endpoints."""
    if kind == ActionKind.MACHINE_ENABLE:
        return "setEnabled", {"ID": target_id, "status": True}
    if kind == ActionKind.MACHINE_DISABLE:
        return "setEnabled", {"ID": target_id, "status": False}
    if kind == ActionKind.SWITCH_ON:
        return "setSwitches", {"ID": target_id, "status": True}
    if kind == ActionKind.SWITCH_OFF:
        return "setSwitches", {"ID": target_id, "status": False}
    raise ValueError(f"no backend mapping for {kind.value}")


class Executor:
    def __init__(self, client: FrmClient, clock: Clock, st: ControlState, safety: SafetyConfig, emit: Emit, current_seq: Callable[[], int]) -> None:
        self.client = client
        self.clock = clock
        self.st = st
        self.safety = safety
        self.emit = emit
        self.current_seq = current_seq
        self._lock = asyncio.Lock()

    async def execute(self, p: Proposal) -> Command:
        st = self.st
        spec = SPECS[p.kind]
        assert spec.expect is not None
        attr, value = spec.expect
        target_id = st.resolved[p.target]
        endpoint, body = backend_call(p.kind, target_id)
        async with self._lock:
            now = self.clock.now()
            cmd = Command(new_id("cmd"), p, p.target, target_id, endpoint, body, attr, value, now, now + self.safety.verify_timeout_s, after_seq=self.current_seq())
            if st.stopped and not p.params.get("stop_restore"):
                cmd.status = "cancelled"
                self.emit("command_cancelled", **cmd.to_dict(), reason="stopped before send")
                return cmd
            st.pending[p.target] = cmd
            st.writes.append(now)
            st.history.append(cmd)
            self.emit("command_sent", **cmd.to_dict())
            try:
                resp = await self.client.post(endpoint, body)
                cmd.response = resp
                errs = [r.get("Error") for r in resp if isinstance(r, dict) and r.get("Error")] if isinstance(resp, list) else []
                if errs:
                    cmd.status, cmd.error = "error", "; ".join(map(str, errs))
                    st.pending.pop(p.target, None)
                else:
                    cmd.status = "sent"
            except FrmError as e:
                cmd.error = str(e)
                if e.kind in ("timeout", "connection"):
                    # the write may or may not have been applied: keep it pending and let
                    # readback decide. Never resend automatically.
                    cmd.status = "uncertain"
                else:
                    cmd.status = "error"
                    st.pending.pop(p.target, None)
            self.emit("command_response", id=cmd.id, decision_id=p.decision_id, source=p.source, alias=p.target, status=cmd.status, error=cmd.error, response=cmd.response)
            return cmd


class Verifier:
    def __init__(self, st: ControlState, safety: SafetyConfig, emit: Emit) -> None:
        self.st = st
        self.safety = safety
        self.emit = emit

    def update(self, snap: Snapshot, now: float) -> list[Command]:
        """Resolve pending commands against an observation started after they were sent."""
        st = self.st
        done: list[Command] = []
        for alias, cmd in list(st.pending.items()):
            obs = st.observe(snap, alias)
            after = snap.seq > cmd.after_seq
            if after and obs == cmd.value:
                cmd.status, cmd.verified_at = "verified", now
                st.expected[alias] = obs
                st.last_change[alias] = now
                st.harness_set[alias] = True
                del st.pending[alias]
                self.emit("verification", id=cmd.id, decision_id=cmd.proposal.decision_id, source=cmd.proposal.source, alias=alias, result="verified", observed=obs, latency_s=round(now - cmd.sent_at, 3), snapshot_seq=snap.seq)
                done.append(cmd)
            elif now >= cmd.deadline:
                cmd.status = "unverified"
                del st.pending[alias]
                st.suspect[alias] = now + self.safety.suspect_cooldown_s
                if obs is not None:
                    st.expected[alias] = obs
                self.emit("verification", id=cmd.id, decision_id=cmd.proposal.decision_id, source=cmd.proposal.source, alias=alias, result="unverified", observed=obs, expected=cmd.value, latency_s=round(now - cmd.sent_at, 3), snapshot_seq=snap.seq, level="error")
                done.append(cmd)
        return done
