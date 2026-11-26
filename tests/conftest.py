from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pytest

from jawn.app import Session, build_session
from jawn.config import Config, load_config
from jawn.control.actions import reset_ids
from jawn.llm.base import Provider
from jawn.sim.host import load_scenario

ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT = ROOT / "experiments" / "plates_and_rods.toml"
SCENARIO = ROOT / "scenarios" / "plates_and_rods.toml"
PLATE = "Build_ConstructorMk1_C_2147480001"
ROD = "Build_ConstructorMk1_C_2147480002"
SMELTER = "Build_SmelterMk1_C_2147470001"
GEN = "Build_GeneratorBiomass_Automated_C_2147460001"
SWITCH = "Build_PowerSwitch_C_2147465001"
INGOT_BOX = "Build_StorageContainerMk1_C_2147481000"
PLATE_BOX = "Build_StorageContainerMk1_C_2147481001"
ROD_BOX = "Build_StorageContainerMk1_C_2147481002"


@pytest.fixture(autouse=True)
def _ids() -> None:
    reset_ids()


@pytest.fixture
def scenario() -> dict[str, Any]:
    return copy.deepcopy(load_scenario(SCENARIO))


def make_cfg(tmp_path: Path | None = None, **over: Any) -> Config:
    cfg = load_config(EXPERIMENT)
    cfg = Config.model_validate(cfg.model_dump())
    # STOP file isolated per test
    cfg.safety.stop_file = str((tmp_path or ROOT / "runs") / "STOP_TEST_UNUSED")
    for dotted, v in over.items():
        obj: Any = cfg
        parts = dotted.split("__")
        for p in parts[:-1]:
            obj = getattr(obj, p)
        setattr(obj, parts[-1], v)
    return cfg


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return make_cfg(tmp_path)


@dataclass
class Scheduler:
    """Fire callbacks at harness times (lockstep clock hook)."""

    sess: Session
    items: list[tuple[float, Callable[[], None]]] = field(default_factory=list)
    fired: list[float] = field(default_factory=list)

    def at(self, t: float, fn: Callable[[], None]) -> None:
        self.items.append((t, fn))
        self.items.sort(key=lambda x: x[0])

    def hook(self, dt: float) -> None:
        now = self.sess.clock.now()
        while self.items and self.items[0][0] <= now:
            t, fn = self.items.pop(0)
            self.fired.append(t)
            fn()


@dataclass
class TruthMonitor:
    """Independent check from simulator truth: max concurrently unpaused constraint members
    during the main phase, excluding intervals where a human enabled one of them."""

    sess: Session
    members: tuple[str, ...] = (PLATE, ROD)  # informational; truth is matched by recipe (IDs may change on reload)
    recipes: tuple[str, ...] = ("Recipe_IronPlate_C", "Recipe_IronRod_C")
    max_harness_running: int = 0
    human_touched: set[str] = field(default_factory=set)  # recipes a human toggled
    samples: int = 0
    _seen_events: int = 0

    def hook(self, dt: float) -> None:
        r = self.sess.runner
        w = self.sess.sim.world  # type: ignore[union-attr]
        for ev in w.events[self._seen_events :]:
            if ev.get("kind") == "set_paused" and ev["id"] in w.machines:
                rec = w.machines[ev["id"]].recipe
                if rec and ev.get("by") == "human":
                    self.human_touched.add(rec.class_name)
                elif rec and ev.get("by") == "frm":
                    self.human_touched.discard(rec.class_name)  # harness took control again
        self._seen_events = len(w.events)
        if r.status != "running":
            return
        running = [m for m in w.machines.values() if m.recipe and m.recipe.class_name in self.recipes and not m.paused]
        if any(m.recipe.class_name in self.human_touched for m in running):  # type: ignore[union-attr]
            return
        self.samples += 1
        self.max_harness_running = max(self.max_harness_running, len(running))


async def session(cfg: Config, scenario: dict[str, Any], controller: str = "agents", provider: Provider | None = None, tmp_path: Path | None = None, **kw: Any) -> tuple[Session, Scheduler, TruthMonitor]:
    sess = await build_session(cfg, controller=controller, provider=provider, sim_scenario=scenario, run_dir=tmp_path, **kw)
    sch = Scheduler(sess)
    tm = TruthMonitor(sess)
    sess.clock.on_advance(sch.hook)  # type: ignore[attr-defined]
    sess.clock.on_advance(tm.hook)  # type: ignore[attr-defined]
    return sess, sch, tm


def human(sess: Session, **spec: Any) -> Callable[[], None]:
    from jawn.sim.frm_server import human_action

    return lambda: human_action(sess.sim.runtime, spec)  # type: ignore[union-attr]


def events(sess: Session, *types: str) -> list[dict[str, Any]]:
    return sess.runner.log.of_type(*types)
