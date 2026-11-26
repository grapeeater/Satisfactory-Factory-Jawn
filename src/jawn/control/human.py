"""Human priority: override detection, in-game chat commands, emergency stop requests."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import SafetyConfig
from ..observe.snapshot import Snapshot
from .state import ControlState, Lock


class OverrideDetector:
    """Any change of a controlled target's state that the harness did not command is treated
    as a human action: the target is locked for ``human_lock_s`` and the change is adopted
    as the new expected state (the harness never 'corrects' a human)."""

    def __init__(self, safety: SafetyConfig) -> None:
        self.safety = safety

    def update(self, snap: Snapshot, st: ControlState, now: float) -> list[dict[str, Any]]:
        events = []
        for alias in st.bindings:
            obs = st.observe(snap, alias)
            if obs is None:
                continue
            if alias not in st.expected:
                st.expected[alias] = obs
                st.initial.setdefault(alias, obs)
                st.harness_set.setdefault(alias, False)
                continue
            if alias in st.pending:
                continue
            if obs != st.expected[alias]:
                prev = st.expected[alias]
                st.locks[alias] = Lock(now + self.safety.human_lock_s, "external change detected (human priority)")
                st.expected[alias] = obs
                st.harness_set[alias] = False
                events.append({"alias": alias, "from": prev, "to": obs, "locked_until": round(now + self.safety.human_lock_s, 3)})
        return events


@dataclass
class ChatCommand:
    verb: str
    args: list[str]
    sender: str
    message: str


class ChatCommands:
    VERBS = {"stop", "release", "hold", "pause", "resume", "status"}

    def __init__(self, prefix: str = "!jawn", own_sender: str = "Jawn") -> None:
        self.prefix = prefix.lower()
        self.own_sender = own_sender
        self._seen: set[tuple[float, str, str]] = set()
        self._primed = False

    def update(self, snap: Snapshot) -> list[ChatCommand]:
        out: list[ChatCommand] = []
        if "chat" not in snap.fresh:
            return out  # no fresh chat this poll
        if not snap.chat and not self._primed:
            self._primed = True
            return out
        for c in snap.chat:
            key = (c.timestamp, c.sender, c.message)
            if key in self._seen:
                continue
            self._seen.add(key)
            if not self._primed:
                continue  # messages that existed before the run started are history, not commands
            if c.sender == self.own_sender:
                continue
            words = c.message.strip().split()
            if len(words) >= 2 and words[0].lower() == self.prefix and words[1].lower() in self.VERBS:
                out.append(ChatCommand(words[1].lower(), words[2:], c.sender, c.message))
        self._primed = True
        return out


@dataclass
class StopRequest:
    reason: str
    source: str
    mode: str  # "hold" | "restore"


@dataclass
class StopController:
    default_mode: str = "restore"
    stop_file: Path | None = None
    request: StopRequest | None = None
    history: list[StopRequest] = field(default_factory=list)

    def trigger(self, reason: str, source: str, mode: str | None = None) -> StopRequest:
        if self.request is None:
            self.request = StopRequest(reason, source, mode or self.default_mode)
            self.history.append(self.request)
        return self.request

    def check_file(self) -> StopRequest | None:
        if self.stop_file is not None and self.stop_file.exists():
            mode = None
            try:
                text = self.stop_file.read_text(encoding="utf-8").strip().lower()
                if text in ("hold", "restore"):
                    mode = text
            except OSError:
                pass
            return self.trigger(f"stop file {self.stop_file} present", "stop_file", mode)
        return None

    @property
    def stopped(self) -> bool:
        return self.request is not None
