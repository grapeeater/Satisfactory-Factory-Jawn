"""Assemble runs from configuration (shared by CLI, dashboard and tests)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .agents.controller import AgentController
from .baseline.policy import BaselineController
from .clock import Clock, LockstepClock, WallClock
from .config import Config
from .frm.client import FrmClient
from .llm.base import Provider
from .llm.budget import BudgetMeter
from .llm.deepseek import DeepSeekProvider
from .llm.mock import MockProvider, ReplayProvider
from .runner import Runner, RunResult
from .sim.host import SimHost, load_scenario


def make_provider(cfg: Config) -> Provider:
    p = cfg.llm.provider
    if p == "mock":
        m = cfg.llm.mock
        return MockProvider(cfg.llm.deepseek.pricing, m.latency_s, m.seed, m.simulate_usage, cfg.llm.deepseek.max_tokens)
    if p == "deepseek":
        return DeepSeekProvider(cfg.llm.deepseek)
    if p == "replay":
        if not cfg.llm.replay_log:
            raise ValueError("llm.replay_log is required for the replay provider")
        return ReplayProvider(cfg.resolve(cfg.llm.replay_log))
    raise ValueError(f"provider {p!r} must be constructed explicitly (e.g. scripted, in tests)")


def make_budget(cfg: Config) -> BudgetMeter:
    b = cfg.budget
    return BudgetMeter(b.max_usd, b.simulated_max_usd, b.max_calls, b.max_calls_per_agent)


@dataclass
class Session:
    cfg: Config
    clock: Clock
    runner: Runner
    sim: SimHost | None

    async def run(self) -> RunResult:
        try:
            return await self.runner.run()
        finally:
            if self.sim is not None:
                await self.sim.stop()


async def build_session(
    cfg: Config,
    *,
    use_sim: bool = True,
    lockstep: bool = True,
    speed: float = 1.0,
    controller: str | None = None,
    provider: Provider | None = None,
    run_dir: Path | None = None,
    run_id: str | None = None,
    sim_scenario: dict[str, Any] | None = None,
    seed: int | None = None,
    observe_only: bool = False,
) -> Session:
    clock: Clock
    sim: SimHost | None = None
    if lockstep:
        if not use_sim:
            raise ValueError("lockstep mode requires the simulator")
        clock = LockstepClock()
    else:
        clock = WallClock(speed if use_sim else 1.0)
    if use_sim:
        scn = sim_scenario if sim_scenario is not None else load_scenario(cfg.resolve(cfg.sim.scenario or ""))
        sim = await SimHost(scn, cfg.sim.token, seed=cfg.sim.seed if seed is None else seed).start()
        if lockstep:
            sim.attach_lockstep(clock)  # type: ignore[arg-type]
        else:
            sim.start_realtime(speed)
        base_url, token = sim.base_url, cfg.sim.token
    else:
        base_url, token = cfg.frm.base_url, os.environ.get(cfg.frm.token_env, "")
    client = FrmClient(base_url, token, cfg.frm.read_endpoints, cfg.frm.write_endpoints, cfg.frm.timeout_s, cfg.frm.get_retries, cfg.frm.allow_remote)
    budget = make_budget(cfg)
    kind = controller or cfg.experiment.controller
    holder: dict[str, Runner] = {}

    def emit(*a: Any, **k: Any) -> Any:
        return holder["r"].log.emit(*a, **k)

    if kind == "baseline":
        ctrl: Any = BaselineController(sorted({t for a in cfg.agents for t in a.owns}))
    else:
        prov = provider or make_provider(cfg)
        if lockstep and getattr(prov, "paid", False) and getattr(prov, "paid_calls_enabled", lambda: True)():
            # lockstep advances game time as fast as the CPU allows, so a real network call
            # would land hundreds of game-seconds late; real providers need a wall clock
            if sim is not None:
                await sim.stop()
            raise ValueError("a paid/real model provider needs wall-clock time: use --realtime")
        ctrl =AgentController(cfg, prov, budget, clock, emit, cfg.llm.deepseek.mode)
    runner = Runner(cfg, clock, client, ctrl, budget, run_dir, run_id, observe_only=observe_only)
    holder["r"] = runner
    return Session(cfg, clock, runner, sim)
