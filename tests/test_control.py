"""Validator, arbiter, override detection and chat commands on hand-built snapshots."""

from jawn.config import ConstraintConfig, SafetyConfig, TargetBinding
from jawn.control.actions import FRM_CAPABILITIES, ActionKind, Proposal
from jawn.control.arbiter import Arbiter
from jawn.control.human import ChatCommands, OverrideDetector, StopController
from jawn.control.state import Command, ControlState, Lock
from jawn.control.validator import Validator
from jawn.observe.snapshot import ChatObs, CircuitObs, Health, MachineObs, Snapshot

SAFETY = SafetyConfig(min_dwell_s=10, max_decision_age_s=30, max_writes_per_min=3, human_lock_s=60, power_margin=0.05)


def mobs(id, paused, producing=False, circuit=2, power=4.0, cls="Build_ConstructorMk1_C", recipe="Recipe_IronPlate_C", loc=(0.0, 0.0, 0.0)):
    return MachineObs(id, "Constructor", cls, "x", recipe, paused, producing, True, circuit, power if producing else 0.0, power, 100.0, 100.0, {"Desc_IronIngot_C": 50}, {}, {}, {}, location=loc)


def snap(seq=1, t=100.0, health=Health.OK, cap=30.0, used=0.0, fuse=False, **ms):
    s = Snapshot(seq, t, t, 1000.0, False, machines={m.id: m for m in ms.values()}, circuits={2: CircuitObs(2, used, cap, 12.0, fuse)})
    s.health = health
    s.fresh = {"machines", "power", "session"}
    return s


def state():
    st = ControlState({"plate": TargetBinding(id="P", class_name="Build_ConstructorMk1_C", recipe_class="Recipe_IronPlate_C", location=(0.0, 0.0, 0.0)), "rod": TargetBinding(id="R"), "smelter": TargetBinding(id="S")})
    return st


def validator():
    return Validator(SAFETY, {"a": {"plate"}, "b": {"rod"}}, {"a": {ActionKind.MACHINE_ENABLE, ActionKind.MACHINE_DISABLE}, "b": {ActionKind.MACHINE_ENABLE, ActionKind.MACHINE_DISABLE}}, FRM_CAPABILITIES)


def prop(src, kind, target, seq=1, t=100.0, observed=None, prio=0.5, **kw):
    return Proposal(src, kind, target, "r", seq, t, t, observed_state=observed, priority=prio, **kw)


S = snap(plate=mobs("P", True), rod=mobs("R", True), smelter=mobs("S", False, True, cls="Build_SmelterMk1_C"))


def code(p, s=S, st=None, now=100.0):
    return validator().validate(p, s, st or state(), now).code


def test_validation_codes():
    E, D = ActionKind.MACHINE_ENABLE, ActionKind.MACHINE_DISABLE
    assert code(prop("a", E, "plate", observed=True)) == "OK"
    assert code(prop("a", ActionKind.WAIT, "")) == "WAIT"
    assert code(prop("a", E, "rod")) == "NOT_OWNER"
    assert code(prop("a", E, "smelter")) == "NOT_OWNER"  # Dave's machine: nobody owns it
    assert code(prop("a", E, "ghost")) == "UNKNOWN_TARGET"
    assert code(prop("a", ActionKind.MACHINE_SET_RECIPE, "plate")) == "UNSUPPORTED_CAPABILITY"
    assert code(prop("a", ActionKind.SWITCH_ON, "plate")) == "WRONG_TARGET_KIND"
    assert code(prop("a", D, "plate")) == "NOOP"
    assert code(prop("a", E, "plate", t=50.0)) == "STALE_DECISION"
    assert code(prop("a", E, "plate", observed=False)) == "STATE_CHANGED"
    assert code(prop("a", E, "plate"), s=snap(health=Health.GAME_PAUSED, plate=mobs("P", True))) == "HEALTH"
    assert code(prop("a", E, "plate"), s=snap(health=Health.STALE, plate=mobs("P", True))) == "HEALTH"
    assert code(prop("a", E, "plate"), s=snap(health=Health.DEGRADED, plate=mobs("P", True))) == "OK"


def test_validation_state_rules():
    E = ActionKind.MACHINE_ENABLE
    st = state()
    st.locks["plate"] = Lock(200.0, "human")
    assert code(prop("a", E, "plate"), st=st) == "HUMAN_LOCK"
    st = state()
    st.pending["plate"] = Command("c", prop("a", E, "plate"), "plate", "P", "setEnabled", {}, "paused", False, 99.0, 120.0)
    assert code(prop("a", E, "plate"), st=st) == "PENDING_COMMAND"
    st = state()
    st.last_change["plate"] = 95.0
    assert code(prop("a", E, "plate"), st=st) == "DWELL"
    st = state()
    st.suspect["plate"] = 130.0
    assert code(prop("a", E, "plate"), st=st) == "SUSPECT"
    st = state()
    st.writes.extend([70.0, 80.0, 90.0])
    assert code(prop("a", E, "plate"), st=st) == "RATE_LIMIT"
    st = state()
    st.held = True
    assert code(prop("a", E, "plate"), st=st) == "HELD"
    st = state()
    st.stopped = True
    assert code(prop("a", E, "plate"), st=st) == "STOPPED"
    # system restore may act while stopped, but still respects human locks
    assert code(prop("harness", E, "plate", system=True, params={"stop_restore": True}), st=st) == "OK"
    st.locks["plate"] = Lock(200.0, "human")
    assert code(prop("harness", E, "plate", system=True, params={"stop_restore": True}), st=st) == "HUMAN_LOCK"
    st = state()
    st.missing.add("plate")
    assert code(prop("a", E, "plate"), st=st) == "TARGET_MISSING"


GROUP = [ConstraintConfig(name="one", targets=["plate", "rod"], max=1)]


def test_arbiter_contention_picks_higher_deficit_and_records_conflict():
    arb = Arbiter(GROUP, SAFETY)
    pa = prop("a", ActionKind.MACHINE_ENABLE, "plate", prio=0.4)
    pb = prop("b", ActionKind.MACHINE_ENABLE, "rod", prio=0.9)
    out = arb.arbitrate([pa, pb], S, state(), 100.0)
    assert [p.target for p in out.grants] == ["rod"]
    assert out.denials[0].proposal is pa and out.denials[0].code == "CONSTRAINT_FULL"
    assert out.conflicts and out.conflicts[0]["kind"] == "contention"


def test_arbiter_tie_break_deterministic_and_fair():
    arb = Arbiter(GROUP, SAFETY)
    pa = prop("a", ActionKind.MACHINE_ENABLE, "plate", prio=0.5)
    pb = prop("b", ActionKind.MACHINE_ENABLE, "rod", prio=0.5)
    out = arb.arbitrate([pb, pa], S, state(), 100.0)
    assert [p.source for p in out.grants] == ["a"]  # id order on a full tie
    # next cycle, b has been waiting longer -> wins a tie
    out2 = arb.arbitrate([prop("a", ActionKind.MACHINE_ENABLE, "plate", prio=0.5), prop("b", ActionKind.MACHINE_ENABLE, "rod", prio=0.5)], S, state(), 110.0)
    assert [p.source for p in out2.grants] == ["b"]


def test_arbiter_occupied_slot_and_pending_disable_still_counts():
    arb = Arbiter(GROUP, SAFETY)
    s = snap(plate=mobs("P", False, True), rod=mobs("R", True))
    out = arb.arbitrate([prop("b", ActionKind.MACHINE_ENABLE, "rod")], s, state(), 100.0)
    assert out.denials[0].code == "CONSTRAINT_FULL" and out.conflicts[0]["kind"] == "occupied"
    # plate's disable is granted in the same cycle, but rod must wait for verification
    out = arb.arbitrate([prop("a", ActionKind.MACHINE_DISABLE, "plate"), prop("b", ActionKind.MACHINE_ENABLE, "rod")], s, state(), 100.0)
    assert [p.kind for p in out.grants] == [ActionKind.MACHINE_DISABLE]
    assert out.denials[0].code == "CONSTRAINT_FULL"
    # a pending enable occupies the slot too
    st = state()
    st.pending["plate"] = Command("c", prop("a", ActionKind.MACHINE_ENABLE, "plate"), "plate", "P", "setEnabled", {}, "paused", False, 99.0, 120.0)
    out = arb.arbitrate([prop("b", ActionKind.MACHINE_ENABLE, "rod")], S, st, 100.0)
    assert out.denials and out.denials[0].code == "CONSTRAINT_FULL"


def test_unknown_state_counts_as_running():
    """Regression: a machine missing from observations may still be running in game."""
    arb = Arbiter(GROUP, SAFETY)
    st = state()
    st.missing.add("plate")
    s = snap(rod=mobs("R", True))  # plate not observed at all
    out = arb.arbitrate([prop("b", ActionKind.MACHINE_ENABLE, "rod")], s, st, 100.0)
    assert not out.grants and out.denials[0].code == "CONSTRAINT_FULL"


def test_arbiter_power_rules():
    arb = Arbiter([], SAFETY)
    p = prop("a", ActionKind.MACHINE_ENABLE, "plate")
    out = arb.arbitrate([p], snap(fuse=True, plate=mobs("P", True)), state(), 100.0)
    assert out.denials[0].code == "POWER_FUSE"
    out = arb.arbitrate([p], snap(cap=10.0, used=6.0, plate=mobs("P", True)), state(), 100.0)
    assert out.denials[0].code == "POWER_HEADROOM"  # 6 + 4 > 9.5
    out = arb.arbitrate([p, prop("b", ActionKind.MACHINE_ENABLE, "rod")], snap(cap=11.0, used=4.0, plate=mobs("P", True), rod=mobs("R", True)), state(), 100.0)
    assert len(out.grants) == 1 and out.denials[0].code == "POWER_HEADROOM"  # 4+4 ok, 4+4+4 > 10.45


def test_human_caused_violation_reported_not_fixed():
    arb = Arbiter(GROUP, SAFETY)
    s = snap(plate=mobs("P", False, True), rod=mobs("R", False, True))
    st = state()
    st.harness_set.update({"plate": True, "rod": False})
    out = arb.arbitrate([], s, st, 100.0)
    assert out.violations and out.violations[0]["caused_by_human"] == ["rod"]
    assert not out.grants
    assert not arb.arbitrate([], s, st, 105.0).violations  # reported once


def test_override_detector_locks_and_adopts():
    det = OverrideDetector(SAFETY)
    st = state()
    det.update(S, st, 100.0)
    assert st.expected["plate"] is True and st.initial["plate"] is True
    s2 = snap(plate=mobs("P", False, True), rod=mobs("R", True), smelter=mobs("S", False, True))
    ev = det.update(s2, st, 105.0)
    assert ev and ev[0]["alias"] == "plate" and st.locked("plate", 106.0)
    assert st.expected["plate"] is False and st.harness_set["plate"] is False
    assert not det.update(s2, st, 110.0)  # adopted, not re-reported
    assert st.locked("plate", 170.0) is None  # lock expires


def test_override_ignores_targets_with_pending_commands():
    det = OverrideDetector(SAFETY)
    st = state()
    det.update(S, st, 100.0)
    st.pending["plate"] = Command("c", prop("a", ActionKind.MACHINE_ENABLE, "plate"), "plate", "P", "setEnabled", {}, "paused", False, 100.0, 115.0)
    assert not det.update(snap(plate=mobs("P", False, True)), st, 101.0)


def test_rebind_by_class_recipe_location():
    st = state()
    s = snap(plate=mobs("P2", True, loc=(10.0, 0.0, 0.0)), rod=mobs("R", True), smelter=mobs("S", False))
    ev = st.resolve(s)
    assert any(e["type"] == "target_rebound" and e["new_id"] == "P2" for e in ev)
    assert st.resolved["plate"] == "P2"
    st2 = state()
    s2 = snap(plate=mobs("P2", True, loc=(5000.0, 0.0, 0.0)), rod=mobs("R", True), smelter=mobs("S", False))
    ev2 = st2.resolve(s2)
    assert any(e["type"] == "target_missing" for e in ev2) and "plate" in st2.missing


def test_chat_commands_ignore_history_and_self():
    cc = ChatCommands("!jawn", own_sender="Jawn")
    s = snap()
    s.fresh = {"chat"}
    s.chat = [ChatObs(1.0, "Dave", "Player", "!jawn stop")]  # existed before the run: history
    assert cc.update(s) == []
    s.chat = s.chat + [ChatObs(2.0, "Jawn", "System", "!jawn stop"), ChatObs(3.0, "Dave", "Player", "!JAWN Hold now"), ChatObs(4.0, "Dave", "Player", "jawn stop")]
    out = cc.update(s)
    assert [(c.verb, c.args) for c in out] == [("hold", ["now"])]
    assert cc.update(s) == []  # not repeated


def test_stop_controller_file_modes(tmp_path):
    f = tmp_path / "STOP"
    sc = StopController("restore", f)
    assert sc.check_file() is None
    f.write_text("hold")
    req = sc.check_file()
    assert req and req.mode == "hold" and sc.stopped
    sc.trigger("again", "x")
    assert len(sc.history) == 1  # first request wins
