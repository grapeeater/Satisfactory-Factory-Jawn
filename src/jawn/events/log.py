"""Append-only structured event log (JSONL) with an in-memory ring for the dashboard.

Event types (``type`` field) form the audit trail:
  run_start, run_end, snapshot, health, alert, target_rebound/missing,
  agent_wake, llm_request, llm_response, llm_error, decision, invalid_output,
  message, proposal, validation, arbitration, conflict, constraint_violation,
  command_sent, command_response, command_cancelled, verification, outcome_anomaly,
  human_override, chat_command, stop, restore, budget
"""

from __future__ import annotations

import json
import time
from collections import deque
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from ..clock import Clock

SECRET_KEYS = {"token", "authorization", "x-frm-authorization", "api_key", "apikey", "password", "secret"}


def redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: ("<redacted>" if str(k).lower() in SECRET_KEYS else redact(v)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact(v) for v in obj]
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, (set, frozenset)):
        return sorted(redact(v) for v in obj)
    return obj


class EventLog:
    def __init__(self, clock: Clock, path: Path | None = None, ring: int = 5000, run_id: str = "") -> None:
        self.clock = clock
        self.path = path
        self.run_id = run_id
        self.seq = 0
        self.ring: deque[dict[str, Any]] = deque(maxlen=ring)
        self.counts: dict[str, int] = {}
        self._fh = None
        self.listeners: list[Callable[[dict[str, Any]], None]] = []
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(path, "a", encoding="utf-8")

    def emit(self, type: str, **data: Any) -> dict[str, Any]:
        self.seq += 1
        ev = {"seq": self.seq, "type": type, "t": round(self.clock.now(), 3), "wall": round(time.time(), 3), **redact(data)}
        self.ring.append(ev)
        self.counts[type] = self.counts.get(type, 0) + 1
        if self._fh is not None:
            self._fh.write(json.dumps(ev, default=str, separators=(",", ":")) + "\n")
            self._fh.flush()
        for cb in self.listeners:
            cb(ev)
        return ev

    def since(self, seq: int, limit: int = 500) -> list[dict[str, Any]]:
        out = [e for e in self.ring if e["seq"] > seq]
        return out[:limit]

    def of_type(self, *types: str) -> list[dict[str, Any]]:
        return [e for e in self.ring if e["type"] in types]

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


def read_events(path: Path) -> list[dict[str, Any]]:
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out
