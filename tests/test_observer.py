"""Observation parsing (tolerant) and health detection."""

import pytest

from jawn.clock import LockstepClock
from jawn.config import SafetyConfig
from jawn.frm.client import FrmClient
from jawn.observe.observer import Observer
from jawn.observe.snapshot import Health, parse_machines, parse_power
from jawn.sim.host import SimHost

from conftest import PLATE

EPS = ["getFactory", "getStorageInv", "getPower", "getSwitches", "getSessionInfo", "getChatMessages"]


@pytest.fixture
async def rig(scenario):
    clock = LockstepClock()
    h = await SimHost(scenario, "tok").start()
    h.attach_lockstep(clock)
    c = FrmClient(h.base_url, "tok", EPS, [], timeout_s=0.3, get_retries=0)
    obs = Observer(c, clock, EPS, SafetyConfig(max_snapshot_age_s=20, game_paused_after_s=12))
    yield h, clock, obs
    await c.close()
    await h.stop()


def test_parser_tolerates_garbage():
    errs: list[str] = []
    ms = parse_machines([{"ID": "A", "IsPaused": "false", "IsProducing": 1, "PowerInfo": "nope"}, {"no": "id"}, "junk", {"ID": "B", "InputInventory": [{"ClassName": "X", "Amount": "7"}, None]}], errs)
    assert ms["A"].paused is False and ms["A"].producing is True
    assert ms["B"].inputs == {"X": 7}
    assert errs  # problems recorded, nothing raised
    assert parse_machines({"oops": 1}, errs) == {}
    assert parse_power([{"PowerCapacity": 5}], errs) == {}


def test_unknown_fields_are_none_not_false():
    ms = parse_machines([{"ID": "A"}], [])
    assert ms["A"].paused is None and ms["A"].producing is None


async def test_ok_snapshot(rig):
    h, clock, obs = rig
    s = await obs.poll()
    assert s.health == Health.OK and PLATE in s.machines and s.circuits and s.storages
    await clock.sleep(5)
    s2 = await obs.poll()
    assert s2.seq == s.seq + 1 and s2.game_time and s2.game_time > (s.game_time or 0)


async def test_degraded_carries_over_and_ages(rig):
    h, clock, obs = rig
    await obs.poll()
    h.faults.set_endpoint("getStorageInv", error_rate=1.0)
    await clock.sleep(5)
    s = await obs.poll()
    assert s.health == Health.DEGRADED
    assert s.storages, "last good storage data is carried over"
    assert s.section_age["storages"] == pytest.approx(5.0)
    assert "getStorageInv" in s.errors


async def test_stale_when_machine_data_too_old(rig):
    h, clock, obs = rig
    await obs.poll()
    h.faults.set_endpoint("getFactory", error_rate=1.0)
    for _ in range(5):
        await clock.sleep(5)
        s = await obs.poll()
    assert s.health == Health.STALE


async def test_down_and_not_found(rig):
    h, clock, obs = rig
    h.faults.apply({"not_found_all": True})
    s = await obs.poll()
    assert s.health == Health.NOT_FOUND
    h.faults.apply({"clear": True, "endpoints": {"*": {"error_rate": 1.0}}})
    s = await obs.poll()
    assert s.health == Health.DOWN  # never had machine data


async def test_game_paused_flag_and_frozen_clock(rig):
    h, clock, obs = rig
    await obs.poll()
    h.world.game_paused = True
    await clock.sleep(5)
    assert (await obs.poll()).health == Health.GAME_PAUSED
    h.world.game_paused = False
    await clock.sleep(5)
    assert (await obs.poll()).health == Health.OK
    # stale cache: every endpoint frozen -> game time stops advancing -> treated as paused
    h.faults.set_endpoint("*", frozen=True)
    healths = []
    for _ in range(5):
        await clock.sleep(5)
        healths.append((await obs.poll()).health)
    assert healths[0] == Health.OK and healths[-1] == Health.GAME_PAUSED


async def test_chat_only_parsed_when_fresh(rig):
    h, clock, obs = rig
    h.world.add_chat("Dave", "hello")
    s = await obs.poll()
    assert [c.message for c in s.chat] == ["hello"]
    h.faults.set_endpoint("getChatMessages", error_rate=1.0)
    s = await obs.poll()
    assert s.chat == []  # stale chat is never re-delivered as new commands
