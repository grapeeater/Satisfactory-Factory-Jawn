"""Run the simulated FRM server on a local port, driven by a lockstep or realtime clock."""

from __future__ import annotations

import asyncio
import contextlib
import time
import tomllib
from pathlib import Path
from typing import Any

from aiohttp import web

from ..clock import LockstepClock
from .faults import FaultInjector
from .frm_server import SimRuntime, make_app
from .world import build_world


def load_scenario(path: str | Path) -> dict[str, Any]:
    with open(path, "rb") as f:
        return tomllib.load(f)


class SimHost:
    def __init__(self, scenario: dict[str, Any], token: str, host: str = "127.0.0.1", port: int = 0, seed: int = 0, sim_token: str | None = None) -> None:
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("the simulator only binds to loopback")
        self.runtime = SimRuntime(build_world(scenario), FaultInjector(seed))
        self.token = token
        self.sim_token = sim_token or token
        self.host = host
        self.port = port
        self._runner: web.AppRunner | None = None
        self._ticker: asyncio.Task[None] | None = None

    @property
    def world(self):
        return self.runtime.world

    @property
    def faults(self) -> FaultInjector:
        return self.runtime.faults

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    async def start(self) -> "SimHost":
        app = make_app(self.runtime, self.token, self.sim_token)
        self._runner = web.AppRunner(app, access_log=None, handle_signals=False)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        if self.port == 0:
            server = site._server  # type: ignore[attr-defined]
            self.port = server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        return self

    def attach_lockstep(self, clock: LockstepClock) -> None:
        clock.on_advance(self.runtime.advance)

    def start_realtime(self, speed: float = 1.0, tick_s: float = 0.1) -> None:
        async def loop() -> None:
            last = time.monotonic()
            while True:
                await asyncio.sleep(tick_s)
                now = time.monotonic()
                dt = (now - last) * speed
                last = now
                # sub-step so belts/machines behave the same at high speed
                while dt > 1e-9:
                    step = min(0.5, dt)
                    self.runtime.advance(step)
                    dt -= step

        self._ticker = asyncio.create_task(loop())

    async def stop(self) -> None:
        if self._ticker:
            self._ticker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._ticker
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
