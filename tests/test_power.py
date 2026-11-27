"""P4: power management on the power-grid scenario (four agents, switches, backup generator).

Safety is checked from simulator truth (PowerTruth): whenever a harness write *adds* load
(enable / close a switch), the affected grid's pessimistic demand (every unpaused machine at
full power) must fit its capacity, and no load shedding or fuse trip may happen that is not
explained by an external power event (generator change, human switch, fuel running out).
"""

from __future__ import annotations

import copy
import json
import os
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from jawn.config import Config, load_config
from jawn.llm.mock import ScriptedProvider, _decision
from jawn.sim.host import load_scenario

from conftest import events, human, session

ROOT = Path(__file__).resolve().parents[1]
P_EXPERIMENT = ROOT / "experiments" / "power_grid.toml"
P_SCENARIO = ROOT / "scenarios" / "power_grid.toml"
MAIN_GEN = "Build_GeneratorBiomass_Automated_C_2147460001"
BACKUP_GEN = "Build_GeneratorCoal_C_2147460009"
IRON_TIE = "Build_PriorityPowerSwitch_C_2147465002"
COPPER_TIE = "Build_PriorityPowerSwitch_C_2147465003"
BACKUP_TIE = "Build_PowerSwitch_C_2147465009"
SMELTERS = ("Build_SmelterMk1_C_2147470001", "Build_SmelterMk1_C_2147470010")
WIRE, CABLE = "Build_ConstructorMk1_C_2147480010", "Build_ConstructorMk1_C_2147480011"
EXTERNAL = {"human", "generator_out_of_fuel", "reload"}


@pytest.fixture
def pcfg(tmp_path: Path) -> Config:
    cfg = load_config(P_EXPERIMENT)
    cfg = Config.model_validate(cfg.model_dump())
    cfg.safety.stop_file = str(tmp_path / "STOP_TEST_UNUSED")
    return cfg


@pytest.fixture
def pscn() -> dict[str, Any]:
    return copy.deepcopy(load_scenario(P_SCENARIO))


@dataclass
class PowerTruth:
    sess: Any
    window_s: float = 30.0
    overcommits: list[dict[str, Any]] = field(default_factory=list)
    unexplained: list[dict[str, Any]] = field(default_factory=list)
    sheds: int = 0
    fuses: int = 0
    _seen: int = 0
    _last_external: float = -1e9

    def _demand(self, grid: Any) -> float:
        w = self.sess.sim.world
        return sum(m.power_mw for m in w.machines.values() if m.circuit in grid.circuits and not m.paused)

    def hook(self, dt: float) -> None:
        w = self.sess.sim.world
        for ev in w.events[self._seen :]:
            k = ev["kind"]
            if k in EXTERNAL or ev.get("by") == "human":
                self._last_external = ev["t"]
                continue
            recent = ev["t"] - self._last_external <= self.window_s
            if k in ("priority_shed", "fuse_tripped"):
                self.sheds += k == "priority_shed"
                self.fuses += k == "fuse_tripped"
                if not recent:
                    self.unexplained.append(ev)
            elif ev.get("by") == "frm" and ((k == "set_paused" and ev["paused"] is False) or (k == "set_switch" and ev["on"] is True)):
                circuit = w.machines[ev["id"]].circuit if k == "set_paused" else w.switches[ev["id"]].a
                g = w.grid_of(circuit)
                d = self._demand(g)
                if g.capacity > 0 and d > g.capacity + 1e-6 and not recent:
                    self.overcommits.append({**ev, "demand": d, "capacity": g.capacity})
        self._seen = len(w.events)


async def psession(cfg: Config, scn: dict[str, Any], controller: str = "agents", **kw: Any):
    sess, sch, _ = await session(cfg, scn, controller, **kw)
    pt = PowerTruth(sess)
    sess.clock.on_advance(pt.hook)  # type: ignore[attr-defined]
    return sess, sch, pt


def assert_power_safe(pt: PowerTruth) -> None:
    assert not pt.overcommits, f"truth: a harness write overcommitted a grid: {pt.overcommits[:3]}"
    assert not pt.unexplained, f"truth: load shed / fuse trip not explained by an external event: {pt.unexplained[:3]}"


def writes(sess, ids=None):
    return [w for w in sess.sim.runtime.write_log if ids is None or w["body"]["ID"] in ids]


# --------------------------------------------------------------------------- nominal
@pytest.mark.parametrize("controller", ["baseline", "agents"])
async def test_power_grid_meets_targets_without_overload(controller, pcfg, pscn, tmp_path):
    sess, _, pt = await psession(pcfg, pscn, controller, tmp_path=tmp_path)
    res = await sess.run()
    s = res.report["summary"]
    w = sess.sim.world
    assert res.status == "done" and s["targets_met_within_duration"], res.report["alerts"][-5:]
    assert s["unverified"] == 0 and s["verified"] == s["commands_sent"]
    assert_power_safe(pt)
    assert pt.sheds == 0 and pt.fuses == 0, "nothing in this run should overload the plant, including the restore"
    assert res.report["restore"]["complete"], res.report["restore"]
    # backup was used, then taken out again (fuel saved), and is off after restore
    assert 0 < w.generators[BACKUP_GEN].fuel_used_s < s["elapsed_s"]
    assert not w.switches[BACKUP_TIE].on
    assert not writes(sess, SMELTERS), "never touch Dave's smelters"
    switch_writes = [x for x in writes(sess) if x["body"]["ID"].startswith("Build_P")]
    assert switch_writes and all(x["endpoint"] == "setSwitches" for x in switch_writes)
    # Dave's state is back: iron line running, copper constructors parked
    assert not w.machines["Build_ConstructorMk1_C_2147480001"].paused and w.machines[WIRE].paused and w.machines[CABLE].paused
    if controller == "agents":
        ag = res.report["controller"]["agents"]
        assert set(ag) == {"plate_agent", "rod_agent", "copper_agent", "power_agent"}
        assert ag["power_agent"]["calls"] >= 2 and s["llm_calls"] <= 90


async def test_power_denial_leads_to_cooperation(pcfg, pscn):
    """Machine agents denied for power ask the power manager, which brings the backup in."""
    sess, _, pt = await psession(pcfg, pscn, "agents")
    res = await sess.run()
    denials = [e for e in events(sess, "arbitration") if e.get("code") == "POWER_HEADROOM"]
    msgs = [e for e in events(sess, "message") if e["to"] == "power_agent"]
    on = [e for e in events(sess, "command_sent") if e["target_id"] == BACKUP_TIE and e["body"]["status"] is True]
    assert denials and msgs and on
    assert on[0]["sent_at"] >= msgs[0]["t"] - 1e-6, "backup came in after (because of) the request"
    assert_power_safe(pt)


async def test_learned_topology_classifies_switches(pcfg, pscn):
    from jawn.control import power

    sess, sch, _ = await psession(pcfg, pscn, "baseline")
    seen = {}

    def snap_classes():
        seen.update({a: power.classify(sess.runner.st, a) for a in ("backup_tie", "iron_tie")})

    sch.at(30, snap_classes)
    await sess.run()
    assert seen["backup_tie"] == "supply_tie"  # observed open at setup: generation, no machines
    assert seen["iron_tie"] == "unknown"  # never observed open: opening it is refused


# --------------------------------------------------------------------------- failures
@pytest.mark.parametrize("controller", ["baseline", "agents"])
async def test_generator_failure_shed_is_recovered(controller, pcfg, pscn):
    """Main generator loses capacity: the game sheds the iron line (priority 2). The harness
    logs it as load shedding (not a human override), brings the backup in, restores the line
    once it fits, and still meets every target, without any harness-caused overload."""
    sess, sch, pt = await psession(pcfg, pscn, controller)
    sch.at(100, human(sess, action="generator", id=MAIN_GEN, capacity_mw=12.0))
    res = await sess.run()
    s = res.report["summary"]
    assert res.status == "done" and s["targets_met_within_duration"], res.report["alerts"][-6:]
    assert pt.sheds >= 1 and pt.fuses == 0
    sheds = events(sess, "power_shed")
    assert sheds and sheds[0]["alias"] == "iron_tie"
    assert not [e for e in events(sess, "human_override") if e["alias"] == "iron_tie"]
    tl = [m for m in res.report["machine_timeline"] if m["alias"] == "iron_tie"]
    off_t = next(m["t"] for m in tl if m["state"] == "off")
    assert any(m["state"] == "on" and m["t"] > off_t for m in tl), "iron line restored"
    assert_power_safe(pt)


async def test_fuse_trip_advisory_and_recovery(pcfg, pscn):
    """Without priority switches an overload trips the fuse. Nothing can be enabled on a tripped
    grid; the power agent asks the human (once) to reset it; after the reset work resumes."""
    for sw in pscn["switches"]:
        sw["priority"] = 0
    sess, sch, pt = await psession(pcfg, pscn, "agents")
    sch.at(60, human(sess, action="generator", id=MAIN_GEN, capacity_mw=6.0))
    sch.at(150, human(sess, action="generator", id=MAIN_GEN, capacity_mw=20.0))
    sch.at(160, human(sess, action="reset_fuse"))
    res = await sess.run()
    adv = events(sess, "advisory")
    assert len(adv) == 1 and adv[0]["agent"] == "power_agent" and "fuse" in adv[0]["message"].lower()
    assert any("advisory from power_agent" in a["message"] for a in res.report["alerts"])
    granted_while_fused = [e for e in events(sess, "command_sent") if 61 < e["sent_at"] < 159 and e["body"]["status"] is True]
    assert not granted_while_fused, "no load may be added to a tripped grid"
    assert res.report["summary"]["targets_met_within_duration"]
    assert pt.fuses >= 1 and not pt.unexplained


async def test_switch_off_refused_when_it_would_overload(pcfg, pscn):
    """A power agent that wants the backup out while the full load depends on it is refused;
    opening a switch whose sides were never observed is refused too."""
    cfg = pcfg
    for a in cfg.agents:
        if a.id != "power_agent":
            a.personality = "greedy"
    # backup in, let the greedy line agents fill the grid beyond what main power alone carries,
    # then try to take the backup out (and to open a never-observed switch)
    wait = _decision("wait", "none", "watching", "m")
    seq = [_decision("switch_on", "backup_tie", "need power", "m"), wait, wait, wait] + [_decision("switch_off", "backup_tie", "save fuel", "m"), _decision("switch_off", "iron_tie", "test", "m")] * 6
    prov = ScriptedProvider({"power_agent": seq}, latency_s=1.0)

    def mock_for_others(req):
        from jawn.llm.mock import _policy

        return _policy(req.view, req.agent_id, "greedy", random.Random(0))

    for aid in ("plate_agent", "rod_agent", "copper_agent"):
        prov.scripts[aid] = [mock_for_others] * 60
    sess, _, pt = await psession(cfg, pscn, "agents", provider=prov)
    res = await sess.run()
    codes = {e["code"] for e in events(sess, "arbitration") if e.get("source") == "power_agent" and e.get("result") == "denied"}
    assert "POWER_TOPOLOGY_UNKNOWN" in codes
    assert "POWER_HEADROOM" in codes
    assert_power_safe(pt)
    assert pt.sheds == 0 and pt.fuses == 0


async def test_restore_never_overloads(pcfg, pscn):
    """Dave's own state is overloaded (all four constructors enabled on 20 MW). The harness
    parks them in setup; at the end the restore must not recreate an overload: machines it
    cannot safely re-enable are left parked and reported."""
    for m in pscn["machines"]:
        m.pop("paused", None)
    sess, sch, pt = await psession(pcfg, pscn, "baseline")
    res = await sess.run()
    restored = res.report["restore"]["restored"]
    skipped = [a for a, v in restored.items() if v == "skipped:POWER_HEADROOM"]
    assert skipped, restored
    assert any("could overload a grid" in a["message"] for a in res.report["alerts"])
    late = [e for e in sess.sim.world.events if e["kind"] in ("priority_shed", "fuse_tripped") and e["t"] > 30]
    assert not late, "the restore caused an overload"


async def test_stop_mid_run_restores_power_safely(pcfg, pscn, tmp_path):
    sess, sch, pt = await psession(pcfg, pscn, "agents")
    sch.at(120, human(sess, action="chat", message="!jawn stop"))
    res = await sess.run()
    assert res.status == "stopped" and res.report["restore"]["complete"], res.report["restore"]
    w = sess.sim.world
    assert not w.switches[BACKUP_TIE].on and w.switches[IRON_TIE].on and w.switches[COPPER_TIE].on
    assert pt.sheds == 0 and pt.fuses == 0
    assert_power_safe(pt)


async def test_power_agent_budget_fallback_uses_baseline_switching(pcfg, pscn):
    pcfg.budget.max_calls_per_agent = 1
    sess, _, pt = await psession(pcfg, pscn, "agents")
    res = await sess.run()
    assert res.report["controller"]["agents"]["power_agent"]["exhausted"]
    assert [e for e in events(sess, "proposal") if e["source"] == "power_agent" and "[budget fallback]" in e["reason"]]
    assert res.report["summary"]["targets_met_within_duration"]
    assert_power_safe(pt)


async def test_backup_fuel_runs_out(pcfg, pscn):
    """A nearly empty backup: when it dies the game sheds a line; the harness copes."""
    for g in pscn["generators"]:
        if g["id"] == BACKUP_GEN:
            g["fuel_s"] = 60
    sess, _, pt = await psession(pcfg, pscn, "agents")
    res = await sess.run()
    w = sess.sim.world
    assert w.generators[BACKUP_GEN].fuel_s == 0 and not w.generators[BACKUP_GEN].online
    assert res.status == "done" and res.report["summary"]["targets_met_within_duration"]
    assert_power_safe(pt)


async def test_switch_priority_changes_which_line_is_shed(pcfg, pscn):
    """The power agent re-orders shedding (copper line idle -> shed it first, protect iron).
    Invalid priority requests are refused; on stop the original priorities come back."""

    def prio(action, target, value):
        d = json.loads(_decision(action, target, "reorder shedding", "m"))
        d["priority"] = value
        return json.dumps(d)

    wait = _decision("wait", "none", "watching", "m")
    seq = [prio("set_priority", "backup_tie", 3), prio("set_priority", "iron_tie", 9), prio("set_priority", "copper_tie", 5), wait, wait, wait, wait, wait, wait]
    prov = ScriptedProvider({"power_agent": seq}, latency_s=1.0, default=wait)
    for aid in ("plate_agent", "rod_agent", "copper_agent"):
        prov.scripts[aid] = [lambda req: __import__("jawn.llm.mock", fromlist=["_policy"])._policy(req.view, req.agent_id, "cooperative", random.Random(0))] * 80
    sess, sch, pt = await psession(pcfg, pscn, "agents", provider=prov)
    w = sess.sim.world
    seen = {}
    sch.at(90, lambda: seen.update({"iron": w.switches[IRON_TIE].priority, "copper": w.switches[COPPER_TIE].priority}))
    sch.at(100, human(sess, action="generator", id=MAIN_GEN, capacity_mw=12.0))
    res = await sess.run()
    codes = [e["code"] for e in events(sess, "validation") if e.get("source") == "power_agent" and not e["ok"]]
    assert "NOT_PRIORITY_SWITCH" in codes  # the backup tie is a plain switch
    assert seen == {"iron": 2, "copper": 5}, "copper line set to shed first; the out-of-range request was refused"
    assert any("priority 1..8" in e["error"] for e in events(sess, "invalid_output") if e["agent"] == "power_agent"), "priority 9 rejected at parse time"
    shed = [e for e in w.events if e["kind"] == "priority_shed"]
    assert shed and shed[0]["switch"] == COPPER_TIE, "with the new priorities the game sheds copper, not iron"
    assert w.switches[IRON_TIE].priority == 2 and w.switches[COPPER_TIE].priority == 1, "restored"
    assert res.report["restore"]["restored"].get("copper_tie:priority") == "ok"
    assert_power_safe(pt)


async def test_dashboard_and_report_show_power(pcfg, pscn, tmp_path):
    from jawn.dashboard.server import dashboard_state

    sess, sch, _ = await psession(pcfg, pscn, "agents", tmp_path=tmp_path)
    states = []
    sch.at(150, lambda: states.append(dashboard_state(sess)))
    res = await sess.run()
    st = json.loads(json.dumps(states[0], default=str))
    sw = {m["alias"]: m for m in st["machines"] if m["alias"].endswith("_tie")}
    assert set(sw) == {"iron_tie", "copper_tie", "backup_tie"} and all(m["state"] in ("on", "off") for m in sw.values())
    assert "supply_tie" in sw["backup_tie"]["recipe"]
    assert all("potential" in p for p in st["power"])
    pw = res.report["power"]
    assert pw["switch_on_s_main_phase"]["backup_tie"] > 0
    md = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "## Power" in md and "backup_tie" in md


def test_decision_human_advisory_target():
    from jawn.agents.decision import decision_tool, parse_decision
    from jawn.control.actions import ActionKind

    allowed = [ActionKind.SWITCH_ON, ActionKind.SWITCH_OFF]
    tool = decision_tool(allowed, ["backup_tie"], ["plate_agent"])
    p = tool["function"]["parameters"]["properties"]
    assert "human" in p["message_to"]["enum"] and set(p["action"]["enum"]) == {"switch_on", "switch_off", "wait"}
    raw = json.dumps({"action": "wait", "target": "none", "reason": "fuse", "message_to": "Human", "message": "Please reset the fuse", "memory_update": ""})
    d, err = parse_decision(raw, allowed, ["backup_tie"], ["plate_agent"])
    assert err is None and d.message_to == "human" and d.message
    raw2 = json.dumps({"action": "enable", "target": "backup_tie", "reason": "x", "message_to": "none", "message": "", "memory_update": ""})
    assert "not allowed" in parse_decision(raw2, allowed, ["backup_tie"], ["plate_agent"])[1]


def test_config_rejects_wrong_initial_state_kind():
    from pydantic import ValidationError

    data = load_config(P_EXPERIMENT).model_dump()
    data["experiment"]["initial_state"]["backup_tie"] = "disabled"
    with pytest.raises(ValidationError, match="on/off for switches"):
        Config.model_validate(data)


# --------------------------------------------------------------------------- chaos
def power_schedule(sess, sch, rng: random.Random) -> list[str]:
    log = []
    f = sess.sim.faults
    t = 30.0
    while t < 900:
        kind = rng.choice(["gen_drop", "gen_drop", "human_switch", "human_toggle", "read_errors", "freeze", "delay_writes", "ignore_writes", "reload"])
        dur = rng.uniform(20, 120)
        log.append(f"{t:.0f}:{kind}:{dur:.0f}")
        if kind == "gen_drop":
            sch.at(t, human(sess, action="generator", id=MAIN_GEN, capacity_mw=rng.choice([4.0, 10.0, 14.0])))
            sch.at(t + dur, human(sess, action="generator", id=MAIN_GEN, capacity_mw=20.0))
            sch.at(t + dur + 2, human(sess, action="reset_fuse"))
        elif kind == "human_switch":
            sid = rng.choice([IRON_TIE, COPPER_TIE, BACKUP_TIE])
            sch.at(t, lambda sid=sid: human(sess, action="switch", id=sid, on=not sess.sim.world.switches[sid].on)())
        elif kind == "human_toggle":
            rec = rng.choice(["Recipe_IronPlate_C", "Recipe_Wire_C", "Recipe_Cable_C"])
            sch.at(t, lambda rec=rec: human(sess, action="toggle", id=next(m.id for m in sess.sim.world.machines.values() if m.recipe and m.recipe.class_name == rec))())
        elif kind == "read_errors":
            sch.at(t, lambda p=rng.uniform(0.1, 0.5): f.set_endpoint("*", error_rate=p))
            sch.at(t + dur, lambda: f.set_endpoint("*", error_rate=0.0))
        elif kind == "freeze":
            sch.at(t, lambda: f.set_endpoint("*", frozen=True))
            sch.at(t + dur, lambda: f.set_endpoint("*", frozen=False))
        elif kind == "delay_writes":
            sch.at(t, lambda d=rng.uniform(1, 10): f.set_writes(apply_delay_s=d))
            sch.at(t + dur, lambda: f.set_writes(apply_delay_s=0.0))
        elif kind == "ignore_writes":
            sch.at(t, lambda: f.set_writes(ignore=True))
            sch.at(t + dur, lambda: f.set_writes(ignore=False))
        elif kind == "reload":
            sch.at(t, human(sess, action="reload", rename_ids=rng.random() < 0.5))
        t += dur + rng.uniform(10, 60)
    return log


@pytest.mark.parametrize("seed", range(1, int(os.environ.get("JAWN_CHAOS_SEEDS", "8")) + 1))
async def test_power_chaos_invariants(seed, pcfg, pscn):
    rng = random.Random(1000 + seed)
    controller = "baseline" if seed % 3 == 0 else "agents"
    for a in pcfg.agents:
        if a.id != "power_agent":
            a.personality = rng.choice(["cooperative", "cooperative", "greedy", "stubborn", "erratic"])
    sess, sch, pt = await psession(pcfg, pscn, controller)
    plan = power_schedule(sess, sch, rng)
    res = await sess.run()
    assert res.status in ("done", "stopped"), (res.status, plan, res.report["alerts"][-5:])
    assert not any("harness crashed" in a["message"] for a in res.report["alerts"]), plan
    assert_power_safe(pt), plan
    assert not writes(sess, SMELTERS)
    assert not sess.runner.st.pending
    sent = {(e["target_id"], e["body"]["status"]) for e in events(sess, "command_sent")}
    assert all((x["body"]["ID"], x["body"]["status"]) in sent for x in sess.sim.runtime.write_log)
    json.dumps(res.report, default=str)  # report must always serialise
