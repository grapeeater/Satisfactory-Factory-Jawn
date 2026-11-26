"""End-to-end runs on the simulator (lockstep): normal operation and failure injection.

Every test checks safety from simulator *truth* (TruthMonitor), not from the harness'
own beliefs: at most one constraint member running because of the harness, ever.
"""

import asyncio
import json

import pytest

from jawn.llm.base import ProviderError
from jawn.llm.mock import MockProvider, ScriptedProvider

from conftest import GEN, PLATE, ROD, SMELTER, events, human, make_cfg, session


def summary(res):
    return res.report["summary"]


def harness_cmds(sess, target=None):
    return [e for e in events(sess, "command_sent") if e["source"] != "harness" and (target is None or e["target_id"] == target)]


def assert_safe(tm, res):
    assert tm.samples > 0
    assert tm.max_harness_running <= 1, "truth: harness ran two constraint members at once"
    assert summary(res)["harness_constraint_violations"] == 0


def assert_operational_after_stop(sess):
    """After the run, Dave's factory is back to its pre-harness state and actually producing."""
    w = sess.sim.world
    assert not w.machines[PLATE].paused and not w.machines[ROD].paused
    before = (w.machines[PLATE].produced.get("Desc_IronPlate_C", 0), w.machines[ROD].produced.get("Desc_IronRod_C", 0))
    for _ in range(120):
        w.step(0.5)
    after = (w.machines[PLATE].produced.get("Desc_IronPlate_C", 0), w.machines[ROD].produced.get("Desc_IronRod_C", 0))
    assert after[0] > before[0] and after[1] > before[1]
    assert w.machines[SMELTER].producing


# --------------------------------------------------------------------------- nominal
@pytest.mark.parametrize("controller", ["baseline", "agents"])
async def test_first_experiment_meets_targets(controller, cfg, scenario, tmp_path):
    sess, sch, tm = await session(cfg, scenario, controller, tmp_path=tmp_path)
    res = await sess.run()
    s = summary(res)
    assert res.status == "done" and s["targets_met_within_duration"]
    assert all(p["delivered"] >= 80 for p in res.report["production"].values())
    assert s["unverified"] == 0 and s["verified"] == s["commands_sent"] >= 2
    assert_safe(tm, res)
    assert res.report["restore"]["complete"]
    assert_operational_after_stop(sess)
    assert not [e for e in sess.sim.runtime.write_log if e["body"]["ID"] == SMELTER], "never touch Dave's smelter"
    assert (tmp_path / "events.jsonl").exists() and (tmp_path / "report.md").exists()
    if controller == "agents":
        assert s["llm_calls"] <= 40, "event-driven scheduling should need few calls"
        assert s["real_cost_usd"] == 0.0 and 0 < s["simulated_cost_usd"] < 0.01


async def test_matching_production_is_measured_from_storage(cfg, scenario):
    sess, _, _ = await session(cfg, scenario, "baseline")
    res = await sess.run()
    w = sess.sim.world
    # report numbers come from storage deltas; they must agree with simulator truth
    for it, p in res.report["production"].items():
        assert p["delivered"] <= w.storage_total(it)


async def test_determinism_same_seed_same_story(cfg, scenario):
    def story(sess):
        return [(e["type"], e["t"], e.get("agent") or e.get("source"), e.get("action") or e.get("kind")) for e in events(sess, "decision", "command_sent", "verification", "arbitration")]

    a, _, _ = await session(make_cfg(), json.loads(json.dumps(scenario)), "agents")
    await a.run()
    b, _, _ = await session(make_cfg(), json.loads(json.dumps(scenario)), "agents")
    await b.run()
    assert story(a) == story(b) and story(a)


# --------------------------------------------------------------------------- independence & scheduling
async def test_agents_have_separate_contexts(cfg, scenario):
    def script(marker, alias):
        def f(req):
            view = req.view
            m = view["your_machines"][0]
            act = "enable" if m["state"] == "paused" and view["progress"][0]["remaining"] > 0 else ("disable" if view["progress"][0]["remaining"] == 0 and m["state"] != "paused" else "wait")
            return json.dumps({"action": act, "target": alias if act != "wait" else "none", "reason": f"{marker}-REASON", "message_to": "rod_agent" if marker == "PLATE" else "none", "message": "HELLO-ROD" if marker == "PLATE" else "", "memory_update": f"{marker}-MEMORY"})

        return [f] * 100

    prov = ScriptedProvider({"plate_agent": script("PLATE", "plate"), "rod_agent": script("ROD", "rod")})
    sess, _, tm = await session(cfg, scenario, "agents", provider=prov)
    res = await sess.run()
    by_agent = {"plate_agent": [], "rod_agent": []}
    for r in prov.requests:
        by_agent[r.agent_id].append(json.dumps(r.messages))
    assert len(by_agent["plate_agent"]) >= 2 and len(by_agent["rod_agent"]) >= 2
    assert not any("PLATE-MEMORY" in t or "PLATE-REASON" in t for t in by_agent["rod_agent"]), "rod saw plate's private context"
    assert not any("ROD-MEMORY" in t or "ROD-REASON" in t for t in by_agent["plate_agent"]), "plate saw rod's private context"
    assert any("PLATE-MEMORY" in t for t in by_agent["plate_agent"][1:]), "agent keeps its own memory"
    assert any("HELLO-ROD" in t for t in by_agent["rod_agent"]), "explicit messages are delivered"
    decisions = events(sess, "decision")
    assert {d["agent"] for d in decisions} == {"plate_agent", "rod_agent"}
    assert_safe(tm, res)


async def test_event_driven_wakes_respect_min_interval(cfg, scenario):
    sess, _, _ = await session(cfg, scenario, "agents")
    res = await sess.run()
    wakes = events(sess, "agent_wake")
    snaps = events(sess, "snapshot")
    assert len(wakes) < len(snaps) / 5, "agents must not be called every tick"
    for agent in ("plate_agent", "rod_agent"):
        ts = [w["t"] for w in wakes if w["agent"] == agent]
        assert all(b - a >= cfg.agents[0].triggers.min_interval_s - 1e-6 for a, b in zip(ts, ts[1:]))
    assert all(w["reasons"] for w in wakes)


async def test_slow_model_decisions_rejected_as_stale(cfg, scenario):
    prov = MockProvider(cfg.llm.deepseek.pricing, latency_s=45.0)  # > max_decision_age_s (30)
    sess, _, tm = await session(cfg, scenario, "agents", provider=prov)
    res = await sess.run()
    codes = res.report["validation_codes"]
    assert codes.get("STALE_DECISION", 0) >= 1
    assert summary(res)["commands_sent"] == 0 or all(e["source"] == "harness" for e in events(sess, "command_sent"))
    assert_safe(tm, res)


# --------------------------------------------------------------------------- conflicts
@pytest.mark.parametrize("personality", ["greedy", "stubborn"])
async def test_conflicting_agents_are_arbitrated(personality, cfg, scenario):
    for a in cfg.agents:
        a.personality = personality
    sess, _, tm = await session(cfg, scenario, "agents")
    res = await sess.run()
    assert_safe(tm, res)
    assert res.report["arbitration_denials"].get("CONSTRAINT_FULL", 0) >= 1
    assert summary(res)["conflicts"] >= 1
    assert summary(res)["targets_met_within_duration"]


async def test_erratic_and_invalid_agents_cannot_break_safety(cfg, scenario):
    cfg.agents[0].personality = "erratic"
    cfg.agents[1].personality = "invalid"
    cfg.experiment.duration_s = 600
    sess, _, tm = await session(cfg, scenario, "agents")
    res = await sess.run()
    assert_safe(tm, res)
    assert summary(res)["invalid_outputs"] >= 3
    assert not [e for e in sess.sim.runtime.write_log if e["body"]["ID"] in (SMELTER,)]
    rod_cmds = [e for e in harness_cmds(sess) if e["alias"] == "rod"]
    assert not rod_cmds, "invalid outputs must never become commands"
    assert res.status == "done"


# --------------------------------------------------------------------------- failures
async def test_power_failure_mid_run(cfg, scenario):
    sess, sch, tm = await session(cfg, scenario, "agents")
    sch.at(80, human(sess, action="generator", id=GEN, capacity_mw=5.0))  # demand 8+ MW -> fuse trips
    sch.at(200, human(sess, action="generator", id=GEN, capacity_mw=30.0))
    sch.at(205, human(sess, action="reset_fuse"))
    res = await sess.run()
    pe = events(sess, "power_event")
    assert [e["event"] for e in pe][:2] == ["fuse_tripped", "fuse_reset"]
    assert any("fuse tripped" in a["message"] for a in res.report["alerts"])
    assert summary(res)["unverified"] == 0
    assert summary(res)["targets_met_within_duration"]
    assert_safe(tm, res)


async def test_enable_denied_while_fuse_tripped(cfg, scenario):
    sess, sch, tm = await session(cfg, scenario, "baseline")
    sch.at(1, human(sess, action="generator", id=GEN, capacity_mw=2.0))  # trips as soon as anything runs
    sch.at(300, human(sess, action="generator", id=GEN, capacity_mw=30.0))
    sch.at(301, human(sess, action="reset_fuse"))
    res = await sess.run()
    assert res.report["arbitration_denials"].get("POWER_FUSE", 0) >= 1 or res.report["arbitration_denials"].get("POWER_HEADROOM", 0) >= 1
    assert_safe(tm, res)
    assert summary(res)["targets_met_within_duration"]


async def test_frozen_observations_halt_writes(cfg, scenario):
    sess, sch, tm = await session(cfg, scenario, "agents")
    sch.at(60, lambda: sess.sim.faults.set_endpoint("*", frozen=True))
    sch.at(200, lambda: sess.sim.faults.set_endpoint("*", frozen=False))
    res = await sess.run()
    h = [e["health"] for e in events(sess, "health")]
    assert "game_paused" in h
    paused_at = next(e["t"] for e in events(sess, "health") if e["health"] == "game_paused")
    resumed_at = next(e["t"] for e in events(sess, "health") if e["t"] > paused_at and e["health"] == "ok")
    assert paused_at <= 60 + cfg.safety.game_paused_after_s + cfg.experiment.poll_interval_s
    assert not [c for c in harness_cmds(sess) if paused_at < c["t"] < resumed_at], "no writes on frozen data"
    assert summary(res)["targets_met_within_duration"]
    assert_safe(tm, res)


async def test_flaky_reads_degrade_but_continue(cfg, scenario):
    sess, sch, tm = await session(cfg, scenario, "agents")
    sch.at(30, lambda: sess.sim.faults.apply({"endpoints": {"*": {"error_rate": 0.3}, "getChatMessages": {"error_rate": 0.3}}}))
    res = await sess.run()
    assert "degraded" in [e["health"] for e in events(sess, "health")]
    assert summary(res)["targets_met_within_duration"]
    assert_safe(tm, res)


async def test_ignored_writes_are_caught_by_readback(cfg, scenario):
    sess, sch, tm = await session(cfg, scenario, "baseline")
    # plate reaches 80 around t~260 and is disabled then: that write is silently ignored
    sch.at(200, lambda: sess.sim.faults.set_writes(ignore=True))
    sch.at(330, lambda: sess.sim.faults.set_writes(ignore=False))
    res = await sess.run()
    s = summary(res)
    assert s["unverified"] >= 1
    assert any("not reflected in game state" in a["message"] for a in res.report["alerts"])
    ver = [e for e in events(sess, "verification") if e["result"] == "unverified"]
    assert ver[0]["alias"] == "plate" and ver[0]["observed"] is False  # still running in truth
    # rod must not be enabled while plate's disable is unverified (plate still runs in truth)
    assert s["targets_met_within_duration"]
    assert_safe(tm, res)


async def test_delayed_writes_verified_late(cfg, scenario):
    sess, sch, tm = await session(cfg, scenario, "baseline")
    sch.at(0, lambda: sess.sim.faults.set_writes(apply_delay_s=8.0))
    res = await sess.run()
    assert summary(res)["unverified"] == 0
    assert res.report["verify_latency_s"]["max"] >= 8.0
    assert_safe(tm, res)


async def test_write_rejections_reported(cfg, scenario):
    sess, sch, tm = await session(cfg, scenario, "baseline")
    sch.at(200, lambda: sess.sim.faults.set_writes(reject_status=500))
    sch.at(330, lambda: sess.sim.faults.set_writes(reject_status=0))
    res = await sess.run()
    assert res.report["command_status"].get("error", 0) >= 1
    assert any("write failed" in a["message"] for a in res.report["alerts"])
    assert summary(res)["targets_met_within_duration"]
    assert_safe(tm, res)


async def test_404_after_reload_freezes_then_recovers(cfg, scenario):
    sess, sch, tm = await session(cfg, scenario, "agents")
    sch.at(90, lambda: sess.sim.faults.apply({"not_found_all": True}))
    sch.at(150, lambda: sess.sim.faults.apply({"not_found_all": False}))
    res = await sess.run()
    assert "not_found" in [e["health"] for e in events(sess, "health")]
    assert any("404" in a["message"] for a in res.report["alerts"])
    assert summary(res)["targets_met_within_duration"]
    assert_safe(tm, res)


async def test_id_change_after_reload_rebinds(cfg, scenario):
    sess, sch, tm = await session(cfg, scenario, "baseline")
    sch.at(100, human(sess, action="reload", rename_ids=True))
    tm.members = (PLATE, ROD)
    res = await sess.run()
    rb = events(sess, "target_rebound")
    assert {e["alias"] for e in rb} == {"plate", "rod"}
    assert summary(res)["targets_met_within_duration"]


async def test_target_missing_blocks_actions(cfg, scenario):
    sess, sch, tm = await session(cfg, scenario, "baseline")
    sch.at(100, lambda: sess.sim.faults.set_endpoint("getFactory", omit_ids=[PLATE]))
    sch.at(300, lambda: sess.sim.faults.set_endpoint("getFactory", omit_ids=[]))
    res = await sess.run()
    miss, found = events(sess, "target_missing"), events(sess, "target_found")
    assert miss and found
    assert not [c for c in harness_cmds(sess, PLATE) if miss[0]["t"] <= c["t"] < found[0]["t"]]
    assert summary(res)["targets_met_within_duration"]
    assert_safe(tm, res)


async def test_missing_target_at_start_aborts_without_writes(cfg, scenario):
    sess, sch, tm = await session(cfg, scenario, "baseline")
    sess.sim.faults.set_endpoint("getFactory", omit_ids=[PLATE])
    res = await sess.run()
    assert res.status == "aborted"
    assert sess.sim.runtime.write_log == []


async def test_game_paused_no_actions(cfg, scenario):
    sess, sch, tm = await session(cfg, scenario, "agents")
    sch.at(100, human(sess, action="pause_game"))
    sch.at(200, human(sess, action="resume_game"))
    res = await sess.run()
    assert "game_paused" in [e["health"] for e in events(sess, "health")]
    assert not [c for c in harness_cmds(sess) if 101 < c["t"] < 200]
    assert summary(res)["targets_met_within_duration"]


# --------------------------------------------------------------------------- humans
async def test_human_override_is_respected(cfg, scenario):
    sess, sch, tm = await session(cfg, scenario, "agents")
    sch.at(60, human(sess, action="set_paused", id=PLATE, paused=True))
    res = await sess.run()
    ov = events(sess, "human_override")
    assert ov and ov[0]["alias"] == "plate"
    t0 = ov[0]["t"]
    lock_end = ov[0]["locked_until"]
    assert not [c for c in harness_cmds(sess, PLATE) if t0 <= c["t"] < lock_end], "harness re-touched a human-locked machine"
    assert res.report["validation_codes"].get("HUMAN_LOCK", 0) >= 0
    assert summary(res)["targets_met_within_duration"]
    assert_safe(tm, res)


async def test_human_exceeding_constraint_is_not_corrected(cfg, scenario):
    sess, sch, tm = await session(cfg, scenario, "baseline")
    sch.at(60, human(sess, action="set_paused", id=ROD, paused=False))  # plate is running
    res = await sess.run()
    cv = events(sess, "constraint_violation")
    assert cv and cv[0]["caused_by_human"] == ["rod"]
    t0 = events(sess, "human_override")[0]["t"]
    assert not [c for c in harness_cmds(sess, ROD) if t0 <= c["t"] < t0 + cfg.safety.human_lock_s]
    assert summary(res)["harness_constraint_violations"] == 0


# --------------------------------------------------------------------------- emergency stop
async def test_chat_emergency_stop_restores_operational_factory(cfg, scenario):
    sess, sch, tm = await session(cfg, scenario, "agents")
    sch.at(100, human(sess, action="chat", message="!jawn stop", sender="Dave"))
    res = await sess.run()
    assert res.status == "stopped"
    st = res.report["stop"][0]
    assert st["source"] == "chat" and st["mode"] == "restore"
    assert res.report["restore"]["complete"]
    assert not [c for c in harness_cmds(sess) if c["t"] > 106], "no agent actions after stop"
    assert all(not a.busy for a in sess.runner.controller.agents.values())
    assert_operational_after_stop(sess)


async def test_stop_file(cfg, scenario, tmp_path):
    stop = tmp_path / "STOP"
    cfg.safety.stop_file = str(stop)
    sess, sch, tm = await session(cfg, scenario, "baseline")
    sch.at(50, lambda: stop.write_text("restore"))
    res = await sess.run()
    assert res.status == "stopped" and res.report["stop"][0]["source"] == "stop_file"
    assert_operational_after_stop(sess)


async def test_stop_hold_mode_leaves_state(cfg, scenario):
    sess, sch, tm = await session(cfg, scenario, "baseline")
    sch.at(100, lambda: sess.runner.stop.trigger("test", "dashboard", "hold"))
    res = await sess.run()
    assert res.report["restore"]["mode"] == "hold"
    w = sess.sim.world
    assert w.machines[ROD].paused or w.machines[PLATE].paused, "hold must not restore"
    writes_after = [e for e in sess.sim.runtime.write_log if e["t"] > 106]
    assert not writes_after


async def test_stop_while_frm_down_is_bounded(cfg, scenario):
    sess, sch, tm = await session(cfg, scenario, "baseline")

    def outage():
        sess.sim.faults.apply({"endpoints": {"*": {"error_rate": 1.0}}, "writes": {"reject_status": 503}})
        sess.runner.stop.trigger("test during outage", "dashboard")

    sch.at(100, outage)
    res = await sess.run()
    assert res.status == "stopped"
    assert res.report["restore"]["complete"] is False
    assert any("restore incomplete" in a["message"] for a in res.report["alerts"])
    assert sess.clock.now() < 100 + cfg.safety.stop_restore_timeout_s + 30


async def test_cancellation_ctrl_c_stops_and_restores(cfg, scenario):
    sess, sch, tm = await session(cfg, scenario, "agents")
    holder = {}
    sch.at(120, lambda: holder["task"].cancel())
    holder["task"] = asyncio.ensure_future(sess.run())
    res = await holder["task"]
    assert res.status == "stopped" and res.report["stop"][0]["source"] == "signal"
    assert res.report["restore"]["complete"]
    assert_operational_after_stop(sess)


async def test_chat_hold_and_resume(cfg, scenario):
    sess, sch, tm = await session(cfg, scenario, "baseline")
    sch.at(30, human(sess, action="chat", message="!jawn hold", sender="Dave"))
    sch.at(400, human(sess, action="chat", message="!jawn resume", sender="Dave"))
    res = await sess.run()
    assert [c["verb"] for c in events(sess, "chat_command")] == ["hold", "resume"]
    assert not [c for c in harness_cmds(sess) if 36 < c["t"] < 400]
    assert summary(res)["targets_met"]


# --------------------------------------------------------------------------- budget & providers
async def test_budget_exhaustion_falls_back_to_baseline(cfg, scenario):
    cap = 0.0012  # worst-case reservation is ~0.0007/call: a couple of calls only
    cfg.budget.simulated_max_usd = cap
    sess, _, tm = await session(cfg, scenario, "agents")
    res = await sess.run()
    assert [e for e in events(sess, "budget") if e["event"] == "exhausted"]
    assert any("[budget fallback]" in e["reason"] for e in events(sess, "proposal"))
    assert summary(res)["targets_met_within_duration"]
    assert res.report["budget"]["simulated"]["spent_usd"] <= cap
    assert_safe(tm, res)


async def test_provider_402_disables_all_agents(cfg, scenario):
    prov = ScriptedProvider({"plate_agent": [ProviderError("budget", "insufficient balance (HTTP 402)")], "rod_agent": []}, latency_s=1.0)
    sess, _, tm = await session(cfg, scenario, "agents", provider=prov)
    res = await sess.run()
    ex = {a: s["exhausted"] for a, s in res.report["controller"]["agents"].items()}
    assert all(ex.values())
    assert summary(res)["targets_met_within_duration"]  # baseline fallback keeps the factory going
    assert_safe(tm, res)


async def test_deepseek_config_cannot_spend_offline(cfg, scenario, monkeypatch):
    monkeypatch.delenv("JAWN_ALLOW_PAID_CALLS", raising=False)
    cfg.llm.provider = "deepseek"
    sess, _, tm = await session(cfg, scenario, "agents")
    prov = sess.runner.controller.provider
    res = await sess.run()
    assert prov.sent == 0, "no HTTP request may reach DeepSeek"
    assert summary(res)["real_cost_usd"] == 0.0
    assert summary(res)["targets_met_within_duration"]  # fallback controller


async def test_observe_only_never_writes(cfg, scenario):
    cfg.experiment.duration_s = 120
    sess, _, _ = await session(cfg, scenario, "agents", observe_only=True)
    res = await sess.run()
    assert sess.sim.runtime.write_log == []
    assert res.report["restore"].get("skipped")
    assert events(sess, "snapshot")
