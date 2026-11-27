"""Prompt assembly, cache-friendly: [shared constant prefix][agent constant role][dynamic observation].

DeepSeek's KV cache is prefix-based, so the shared part must stay byte-identical across all
agents and calls; nothing time-varying may appear before the observation.
"""

from __future__ import annotations

import json
from typing import Any

from ..config import AgentConfig

SHARED_SYSTEM = """You are one of several independent factory-management agents in the game Satisfactory.
Each agent controls only its own machines and pursues its own objective; other agents have their own objectives.

Rules:
- You see the factory through periodic JSON observations. You may enable (unpause) or disable (pause) machines you own, switch power switches you own on or off, or wait.
- Power: "potential_mw" counts every machine that is not paused at full power. The arbiter refuses changes that could overload a grid. Machines on a grid with no capacity are "unpowered". Fuses can only be reset by the human.
- A deterministic arbiter enforces shared constraints (for example: at most N machines running in a group), power limits and human priority. Requests that break them are denied. Do not repeat a denied request unless the situation changed.
- The human player always has priority. A machine locked by the human must be left alone.
- An action only counts once a later observation verifies it. "pending" means your last command is still being verified.
- Prefer "wait" when nothing needs to change. Every decision costs money.
- Output beyond your delivery targets is waste: it consumes shared inputs and holds shared slots. Once your targets are met, disable your machines unless your objective says otherwise.
- You may send one short message to another agent per decision (to coordinate, ask, or disagree). Messages are not orders.
- message_to "human" sends an advisory to the human player: only for something you cannot do yourself and that needs them (for example a tripped fuse). Never repeat an advisory.
- memory_update is your private note to your future self. It replaces your previous note. Keep it under 200 characters.
- Keep "reason" and "message" under 120 characters each, short and concrete."""

TOOLS_TAIL = "Respond only by calling the decide function."

JSON_TAIL = """Respond with only a json object with exactly these keys:
{"action": "enable|disable|wait", "target": "<your machine alias or none>", "reason": "<short>", "message_to": "<agent id, human, or none>", "message": "<short or empty>", "memory_update": "<short note>"}"""


def role_block(a: AgentConfig, peer_ids: list[str], roles: dict[str, str] | None = None) -> str:
    targets = ", ".join(f"{n} x {item}" for item, n in sorted(a.targets.items())) or "none"
    roles = roles or {}
    peers = ", ".join(f"{p} ({roles[p]})" if p in roles else p for p in sorted(peer_ids)) or "none"
    return (
        f"Your agent id: {a.id}\n"
        f"Your role: {a.role}\n"
        f"Your objective: {a.objective}\n"
        f"Delivery targets: {targets}\n"
        f"Targets you own (aliases): {', '.join(sorted(a.owns))}\n"
        f"Other agents: {peers}"
    )


def build_messages(a: AgentConfig, peer_ids: list[str], view: dict[str, Any], mode: str = "tools_strict", roles: dict[str, str] | None = None) -> list[dict[str, str]]:
    tail = TOOLS_TAIL if mode == "tools_strict" else JSON_TAIL
    system = SHARED_SYSTEM + "\n\n" + tail + "\n\n" + role_block(a, peer_ids, roles)
    user = "Observation json:\n" + json.dumps(view, separators=(",", ":"), sort_keys=False, default=str)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]
