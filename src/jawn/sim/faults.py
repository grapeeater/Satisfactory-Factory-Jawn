"""Fault injection for the simulated FRM server.

Faults can be set from tests (``FaultInjector`` object) or at runtime over HTTP
(``POST /sim/faults``) / from the dashboard. Randomised faults use a seeded RNG so
runs are reproducible.
"""

from __future__ import annotations

import random
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class EndpointFault:
    error_rate: float = 0.0  # probability of an HTTP error
    error_status: int = 500
    delay_s: float = 0.0  # real-time response delay (use > client timeout to cause timeouts)
    malformed_rate: float = 0.0  # probability of returning invalid JSON
    drop_rate: float = 0.0  # probability of closing the connection without a response
    frozen: bool = False  # keep serving the response cached when frozen was set (stale data)
    omit_ids: list[str] = field(default_factory=list)  # leave these objects out of list responses


@dataclass
class WriteFault:
    ignore: bool = False  # accept (200) but do not apply -> tests readback verification
    apply_delay_s: float = 0.0  # game-seconds before the write takes effect
    reject_status: int = 0  # non-zero -> respond with this HTTP status and do nothing


@dataclass
class FaultState:
    endpoints: dict[str, EndpointFault] = field(default_factory=dict)  # "*" applies to all reads
    writes: WriteFault = field(default_factory=WriteFault)
    not_found_all: bool = False  # FRM issue #313: every endpoint 404s after a session reload
    server_down: bool = False  # refuse everything (simulates the web server being stopped)


class FaultInjector:
    def __init__(self, seed: int = 0) -> None:
        self.state = FaultState()
        self.rng = random.Random(seed)
        self.frozen_cache: dict[str, Any] = {}

    # -- read side
    def for_endpoint(self, name: str) -> EndpointFault:
        return self.state.endpoints.get(name) or self.state.endpoints.get("*") or EndpointFault()

    def roll(self, p: float) -> bool:
        return p > 0 and self.rng.random() < p

    # -- control
    def set_endpoint(self, name: str, **kw: Any) -> None:
        cur = self.state.endpoints.get(name, EndpointFault())
        for k, v in kw.items():
            if not hasattr(cur, k):
                raise KeyError(f"unknown endpoint fault field {k!r}")
            setattr(cur, k, v)
        self.state.endpoints[name] = cur
        if not cur.frozen:
            self.frozen_cache.pop(name, None)

    def set_writes(self, **kw: Any) -> None:
        for k, v in kw.items():
            if not hasattr(self.state.writes, k):
                raise KeyError(f"unknown write fault field {k!r}")
            setattr(self.state.writes, k, v)

    def clear(self) -> None:
        self.state = FaultState()
        self.frozen_cache.clear()

    def apply(self, spec: dict[str, Any]) -> None:
        """Apply a JSON fault spec, e.g.
        {"clear": true, "endpoints": {"getFactory": {"error_rate": 0.5}}, "writes": {"ignore": true},
         "not_found_all": false, "server_down": false}"""
        if spec.get("clear"):
            self.clear()
        for name, kw in (spec.get("endpoints") or {}).items():
            self.set_endpoint(name, **kw)
        if spec.get("writes"):
            self.set_writes(**spec["writes"])
        for flag in ("not_found_all", "server_down"):
            if flag in spec:
                setattr(self.state, flag, bool(spec[flag]))

    def describe(self) -> dict[str, Any]:
        return asdict(self.state)
