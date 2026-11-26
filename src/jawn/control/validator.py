"""Schema/authority/safety validation of proposals, before arbitration.

Every proposal gets exactly one ValidationResult with a machine-readable code, so the
event log separates "what the agent chose" from "what the harness allowed".
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import SafetyConfig
from ..observe.snapshot import WRITE_SAFE, Health, Snapshot
from .actions import SPECS, ActionKind, Proposal
from .state import ControlState


@dataclass
class ValidationResult:
    ok: bool
    code: str
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        return {"ok": self.ok, "code": self.code, "detail": self.detail}


OK = ValidationResult(True, "OK")


class Validator:
    def __init__(self, safety: SafetyConfig, authority: dict[str, set[str]], allowed: dict[str, set[ActionKind]], capabilities: frozenset[ActionKind]) -> None:
        self.safety = safety
        self.authority = authority  # source -> aliases it may act on
        self.allowed = allowed  # source -> action kinds it may use
        self.capabilities = capabilities

    def validate(self, p: Proposal, snap: Snapshot, st: ControlState, now: float) -> ValidationResult:
        def no(code: str, detail: str = "") -> ValidationResult:
            return ValidationResult(False, code, detail)

        if p.kind == ActionKind.WAIT:
            return ValidationResult(False, "WAIT", "no action requested")
        restore = bool(p.params.get("stop_restore"))
        if st.stopped and not (p.system and restore):
            return no("STOPPED", "harness is stopped")
        if st.held and not p.system:
            return no("HELD", "harness writes are on hold")
        if p.kind not in self.capabilities:
            return no("UNSUPPORTED_CAPABILITY", f"{p.kind.value} is not supported by the game backend")
        b = st.bindings.get(p.target)
        if b is None:
            return no("UNKNOWN_TARGET", f"no target alias {p.target!r}")
        spec = SPECS[p.kind]
        if spec.target_kind != b.kind:
            return no("WRONG_TARGET_KIND", f"{p.kind.value} needs a {spec.target_kind}, {p.target} is a {b.kind}")
        if not p.system:
            if p.target not in self.authority.get(p.source, set()):
                return no("NOT_OWNER", f"{p.source} has no authority over {p.target}")
            if p.kind not in self.allowed.get(p.source, set()):
                return no("ACTION_NOT_ALLOWED", f"{p.source} may not use {p.kind.value}")
        if snap.health not in WRITE_SAFE:
            return no("HEALTH", f"observations not trustworthy ({snap.health.value})")
        if p.target in st.missing:
            return no("TARGET_MISSING", f"{p.target} not present in latest observation")
        current = st.observe(snap, p.target)
        if current is None:
            return no("TARGET_MISSING", f"{p.target} state unknown")
        if not p.system:
            age = now - p.based_on_time
            if age > self.safety.max_decision_age_s:
                return no("STALE_DECISION", f"decision based on observation {age:.1f}s old (max {self.safety.max_decision_age_s:.0f}s)")
            if p.observed_state is not None and p.observed_state != current:
                return no("STATE_CHANGED", f"{p.target} changed since the decision was made")
        if p.target in st.pending:
            return no("PENDING_COMMAND", f"a command for {p.target} is still being verified")
        lk = st.locked(p.target, now)
        if lk:
            return no("HUMAN_LOCK", f"{p.target} locked until t={lk.until:.0f} ({lk.reason})")
        if st.is_suspect(p.target, now) and not restore:
            return no("SUSPECT", f"{p.target} recently failed verification; cooling down")
        assert spec.expect is not None
        _, want = spec.expect
        if current == want:
            return no("NOOP", f"{p.target} already in requested state")
        if not p.system:
            last = st.last_change.get(p.target)
            if last is not None and now - last < self.safety.min_dwell_s:
                return no("DWELL", f"{p.target} changed {now - last:.1f}s ago (min dwell {self.safety.min_dwell_s:.0f}s)")
        if st.writes_in_last(60.0, now) >= self.safety.max_writes_per_min and not restore:
            return no("RATE_LIMIT", "write rate limit reached")
        return OK


__all__ = ["Validator", "ValidationResult", "Health"]
