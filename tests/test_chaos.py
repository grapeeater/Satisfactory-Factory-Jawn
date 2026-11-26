"""Randomised fault/human/agent chaos: safety invariants must hold for every seed."""

import os
import random

import pytest

from conftest import GEN, PLATE, ROD, SMELTER, events, human, session


def schedule(sess, sch, rng: random.Random) -> list[str]:
    log = []
    f = sess.sim.faults
    t = 20.0
    while t < 1000:
        kind = rng.choice(["read_errors", "freeze", "ignore_writes", "delay_writes", "reject_writes", "human_toggle", "power_cut", "pause_game", "reload", "chat_hold", "omit"])
        dur = rng.uniform(15, 90)
        log.append(f"{t:.0f}:{kind}:{dur:.0f}")
        if kind == "read_errors":
            sch.at(t, lambda p=rng.uniform(0.1, 0.6): f.set_endpoint("*", error_rate=p))
            sch.at(t + dur, lambda: f.set_endpoint("*", error_rate=0.0))
        elif kind == "freeze":
            sch.at(t, lambda: f.set_endpoint("*", frozen=True))
            sch.at(t + dur, lambda: f.set_endpoint("*", frozen=False))
        elif kind == "ignore_writes":
            sch.at(t, lambda: f.set_writes(ignore=True))
            sch.at(t + dur, lambda: f.set_writes(ignore=False))
        elif kind == "delay_writes":
            sch.at(t, lambda d=rng.uniform(1, 20): f.set_writes(apply_delay_s=d))
            sch.at(t + dur, lambda: f.set_writes(apply_delay_s=0.0))
        elif kind == "reject_writes":
            sch.at(t, lambda: f.set_writes(reject_status=503))
            sch.at(t + dur, lambda: f.set_writes(reject_status=0))
        elif kind == "human_toggle":
            recipe = rng.choice(["Recipe_IronPlate_C", "Recipe_IronRod_C"])

            def toggle(recipe=recipe):  # resolve at fire time: IDs may have changed on reload
                w = sess.sim.world
                mid = next(m.id for m in w.machines.values() if m.recipe and m.recipe.class_name == recipe)
                human(sess, action="toggle", id=mid)()

            sch.at(t, toggle)
        elif kind == "power_cut":
            sch.at(t, human(sess, action="generator", id=GEN, capacity_mw=rng.choice([2.0, 5.0, 9.0])))
            sch.at(t + dur, human(sess, action="generator", id=GEN, capacity_mw=30.0))
            sch.at(t + dur + 1, human(sess, action="reset_fuse"))
        elif kind == "pause_game":
            sch.at(t, human(sess, action="pause_game"))
            sch.at(t + dur, human(sess, action="resume_game"))
        elif kind == "reload":
            sch.at(t, human(sess, action="reload", rename_ids=rng.random() < 0.5))
        elif kind == "chat_hold":
            sch.at(t, human(sess, action="chat", message="!jawn hold"))
            sch.at(t + dur, human(sess, action="chat", message="!jawn resume"))
        elif kind == "omit":
            recipe = rng.choice(["Recipe_IronPlate_C", "Recipe_IronRod_C"])
            sch.at(t, lambda recipe=recipe: f.set_endpoint("getFactory", omit_ids=[m.id for m in sess.sim.world.machines.values() if m.recipe and m.recipe.class_name == recipe]))
            sch.at(t + dur, lambda: f.set_endpoint("getFactory", omit_ids=[]))
        t += dur + rng.uniform(5, 60)
    return log


@pytest.mark.parametrize("seed", range(1, int(os.environ.get("JAWN_CHAOS_SEEDS", "12")) + 1))
async def test_chaos_invariants(seed, cfg, scenario):
    rng = random.Random(seed)
    controller = "baseline" if seed % 4 == 0 else "agents"
    for a in cfg.agents:
        a.personality = rng.choice(["cooperative", "cooperative", "greedy", "stubborn", "erratic", "invalid"])
    sess, sch, tm = await session(cfg, scenario, controller)
    plan = schedule(sess, sch, rng)
    res = await sess.run()

    # 1. run terminates cleanly (never crashes)
    assert res.status in ("done", "stopped"), (res.status, plan, res.report["alerts"][-5:])
    assert not any("harness crashed" in a["message"] for a in res.report["alerts"])
    # 2. truth: the harness never ran both constraint members at once
    assert tm.max_harness_running <= 1, plan
    assert res.report["summary"]["harness_constraint_violations"] == 0, plan
    # 3. Dave's machine is never written to
    assert not [w for w in sess.sim.runtime.write_log if SMELTER in str(w["body"].get("ID"))]
    # 4. every harness command reached a terminal state (no dangling pending commands)
    assert not sess.runner.st.pending
    # 5. no model task left running
    if controller == "agents":
        assert all(not a.busy for a in sess.runner.controller.agents.values())
    # 6. every write the game received corresponds to a logged harness command
    sent = events(sess, "command_sent")
    sent_ids = {(e["target_id"], e["body"]["status"]) for e in sent}
    assert len(sess.sim.runtime.write_log) <= len(sent)
    assert all((w["body"]["ID"], w["body"]["status"]) in sent_ids for w in sess.sim.runtime.write_log)
