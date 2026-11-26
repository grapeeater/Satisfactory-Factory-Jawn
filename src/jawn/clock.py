"""Clocks.

All harness components take time from a Clock so the same code runs:
  * live / realtime   -> WallClock (optionally sped up for the simulator)
  * deterministic test -> LockstepClock, where sleeping advances the simulator
"""

from __future__ import annotations

import asyncio
import time
from typing import Callable, Protocol


class Clock(Protocol):
    def now(self) -> float: ...

    async def sleep(self, dt: float) -> None: ...


class WallClock:
    """Real time, scaled by ``speed`` (speed > 1 only makes sense against the simulator)."""

    def __init__(self, speed: float = 1.0) -> None:
        if speed <= 0:
            raise ValueError("speed must be > 0")
        self.speed = speed
        self._t0 = time.monotonic()

    def now(self) -> float:
        return (time.monotonic() - self._t0) * self.speed

    async def sleep(self, dt: float) -> None:
        await asyncio.sleep(max(0.0, dt) / self.speed)


class LockstepClock:
    """Deterministic clock. ``sleep(dt)`` advances time (and any attached simulator)
    in sub-steps of at most ``max_step`` seconds, then yields to the event loop so
    in-flight tasks (e.g. mock model calls) can complete."""

    def __init__(self, max_step: float = 0.5, yields: int = 8) -> None:
        self._t = 0.0
        self.max_step = max_step
        self.yields = yields
        self._hooks: list[Callable[[float], None]] = []

    def on_advance(self, hook: Callable[[float], None]) -> None:
        self._hooks.append(hook)

    def now(self) -> float:
        return self._t

    def advance(self, dt: float) -> None:
        remaining = max(0.0, dt)
        while remaining > 1e-12:
            step = min(self.max_step, remaining)
            self._t += step
            remaining -= step
            for hook in self._hooks:
                hook(step)

    async def sleep(self, dt: float) -> None:
        self.advance(dt)
        for _ in range(self.yields):
            await asyncio.sleep(0)
