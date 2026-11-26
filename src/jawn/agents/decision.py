"""Agent decision contract: strict function schema + defensive parsing of model output."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from ..control.actions import SHORT_NAMES, ActionKind

MAX_REASON = 200
MAX_MESSAGE = 200
MAX_MEMORY = 300

KIND_TO_SHORT = {v: k for k, v in SHORT_NAMES.items()}


@dataclass
class Decision:
    action: ActionKind
    target: str  # alias, or "" for wait
    reason: str
    message_to: str  # "" for none
    message: str
    memory_update: str
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"action": self.action.value, "target": self.target, "reason": self.reason, "message_to": self.message_to or "none", "message": self.message, "memory_update": self.memory_update, "truncated": self.truncated}


def decision_tool(allowed: list[ActionKind], own_targets: list[str], peers: list[str]) -> dict[str, Any]:
    """OpenAI/DeepSeek function definition. Strict mode: every property required,
    additionalProperties false, enums for closed sets (lengths are enforced after parsing,
    since strict mode does not support minLength/maxLength)."""
    actions = sorted({KIND_TO_SHORT[k] for k in allowed if k in KIND_TO_SHORT} | {"wait"})
    return {
        "type": "function",
        "function": {
            "name": "decide",
            "description": "Submit your decision for this observation.",
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": actions, "description": "enable/disable one of your machines, or wait"},
                    "target": {"type": "string", "enum": sorted(own_targets) + ["none"], "description": "your machine alias, or none when waiting"},
                    "reason": {"type": "string", "description": f"why, max {MAX_REASON} chars"},
                    "message_to": {"type": "string", "enum": ["none"] + sorted(peers), "description": "agent to message, or none"},
                    "message": {"type": "string", "description": f"short message to that agent (empty if none), max {MAX_MESSAGE} chars"},
                    "memory_update": {"type": "string", "description": f"your private note for next time (replaces the old one), max {MAX_MEMORY} chars"},
                },
                "required": ["action", "target", "reason", "message_to", "message", "memory_update"],
                "additionalProperties": False,
            },
        },
    }


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_decision(raw: str, allowed: list[ActionKind], own_targets: list[str], peers: list[str]) -> tuple[Decision | None, str | None]:
    """Returns (decision, None) or (None, error). Never raises."""
    if raw is None or not str(raw).strip():
        return None, "empty output"
    text = _FENCE.sub("", str(raw).strip()).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return None, f"not JSON: {e.msg}"
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError as e2:
            return None, f"not JSON: {e2.msg}"
    if not isinstance(data, dict):
        return None, "JSON is not an object"
    expected = {"action", "target", "reason", "message_to", "message", "memory_update"}
    extra = set(data) - expected
    if extra:
        return None, f"unexpected fields {sorted(extra)}"
    action_s = str(data.get("action", "")).strip().lower()
    kind = SHORT_NAMES.get(action_s)
    if kind is None:
        try:
            kind = ActionKind(action_s)
        except ValueError:
            return None, f"unknown action {action_s!r}"
    if kind != ActionKind.WAIT and kind not in allowed:
        return None, f"action {action_s!r} not allowed for this agent"
    target = str(data.get("target", "none")).strip()
    if kind == ActionKind.WAIT:
        target = ""
    elif target not in own_targets:
        return None, f"target {target!r} is not one of your machines {own_targets}"
    msg_to = str(data.get("message_to", "none")).strip()
    if msg_to in ("", "none", "null"):
        msg_to = ""
    elif msg_to not in peers:
        return None, f"message_to {msg_to!r} is not a known agent"
    truncated = False

    def clip(v: Any, n: int) -> str:
        nonlocal truncated
        s = "" if v is None else str(v)
        if len(s) > n:
            truncated = True
            return s[:n]
        return s

    return (
        Decision(kind, target, clip(data.get("reason"), MAX_REASON), msg_to, clip(data.get("message"), MAX_MESSAGE) if msg_to else "", clip(data.get("memory_update"), MAX_MEMORY), truncated),
        None,
    )
