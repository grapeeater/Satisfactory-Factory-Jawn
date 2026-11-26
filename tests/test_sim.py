"""Simulator mechanics."""

from jawn.sim.world import build_world

from conftest import GEN, INGOT_BOX, PLATE, PLATE_BOX, ROD, ROD_BOX, SMELTER, SWITCH


def run(w, seconds, step=0.5):
    t = 0.0
    while t < seconds - 1e-9:
        w.step(step)
        t += step


def test_recipe_rates_match_game(scenario):
    w = build_world(scenario)
    run(w, 120)
    # plates: 2 per 6 s = 20/min; rods: 1 per 4 s = 15/min (input belts fill buffers first)
    assert 36 <= w.machines[PLATE].produced["Desc_IronPlate_C"] <= 40
    assert 28 <= w.machines[ROD].produced["Desc_IronRod_C"] <= 30
    # outputs are carried to storage by belts
    assert w.storages[PLATE_BOX].inventory.count("Desc_IronPlate_C") >= 34
    assert w.storages[ROD_BOX].inventory.count("Desc_IronRod_C") >= 27


def test_pause_freezes_progress_and_resume_continues(scenario):
    w = build_world(scenario)
    run(w, 9)
    m = w.machines[PLATE]
    before = dict(m.produced), m.progress
    w.set_paused(PLATE, True)
    run(w, 60)
    assert dict(m.produced) == before[0] and m.progress == before[1]
    assert m.status == "paused" and not m.producing
    w.set_paused(PLATE, False)
    run(w, 12)
    assert m.produced["Desc_IronPlate_C"] > before[0].get("Desc_IronPlate_C", 0)


def test_starvation_and_blocking(scenario):
    scenario["storages"][0]["items"] = {"Desc_IronIngot_C": 0}
    scenario["supplies"] = []
    w = build_world(scenario)
    run(w, 10)
    assert w.machines[PLATE].status == "starved" and not w.machines[PLATE].producing
    # blocked: no output belt, unlimited input straight into the rod constructor
    s2 = dict(scenario, belts=[b for b in scenario["belts"] if b["id"] != "belt_rod_out"], supplies=[{"id": "feed", "item": "Desc_IronIngot_C", "dst": f"{ROD}:in", "rate_per_min": 60}])
    w2 = build_world(s2)
    run(w2, 4 * 205)
    assert w2.machines[ROD].status == "blocked"
    assert w2.machines[ROD].outputs.count("Desc_IronRod_C") == 200


def test_storage_slot_capacity(scenario):
    w = build_world(scenario)
    inv = w.storages[ROD_BOX].inventory
    assert inv.room("Desc_IronRod_C") == 24 * 200
    assert inv.add("Desc_IronRod_C", 10_000) == 4800
    assert inv.room("Desc_IronPlate_C") == 0


def test_overload_trips_fuse_until_human_reset(scenario):
    w = build_world(scenario)
    w.generators[GEN].capacity_mw = 6.0  # 3 machines x 4 MW = 12 MW demand
    w._recompute_grids()
    run(w, 2)
    assert w.fused, "fuse should trip on overload"
    assert all(not m.producing for m in w.machines.values())
    assert all(m.status == "unpowered" for m in w.machines.values())
    w.generators[GEN].capacity_mw = 30.0
    run(w, 5)
    assert w.fused, "fuse stays tripped until reset"
    w.reset_fuse()
    run(w, 7)
    assert not w.fused and w.machines[PLATE].producing
    assert any(e["kind"] == "fuse_tripped" for e in w.events)


def test_priority_switch_sheds_instead_of_tripping(scenario):
    scenario["switches"][0]["priority"] = 3
    w = build_world(scenario)
    w.generators[GEN].capacity_mw = 6.0
    run(w, 2)
    assert not w.fused
    assert w.switches[SWITCH].on is False
    assert any(e["kind"] == "priority_shed" for e in w.events)
    assert w.machines[PLATE].status == "unpowered"  # shed side has no generation


def test_switch_off_cuts_power(scenario):
    w = build_world(scenario)
    w.set_switch(SWITCH, on=False)
    run(w, 3)
    assert w.machines[SMELTER].status == "unpowered"


def test_game_pause_stops_time(scenario):
    w = build_world(scenario)
    w.game_paused = True
    run(w, 30)
    assert w.game_time == 0.0
    assert not any(m.produced for m in w.machines.values())


def test_reload_renames_ids_and_keeps_links(scenario):
    w = build_world(scenario)
    mapping = w.reload(rename_ids=True)
    assert PLATE not in w.machines and mapping[PLATE] in w.machines
    assert mapping[INGOT_BOX] in w.storages
    run(w, 30)  # belts still connected after the rename
    assert w.machines[mapping[PLATE]].produced


def test_truth_timeline_records_transitions(scenario):
    w = build_world(scenario)
    w.set_paused(PLATE, True, by="human")
    run(w, 2)
    assert any(t["id"] == PLATE and t["paused"] for t in w.timeline)
