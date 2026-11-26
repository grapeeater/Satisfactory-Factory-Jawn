"""Simulated FRM server + FRM client: shapes, auth, allowlists, retries, faults."""

import pytest

from jawn.frm.client import FrmClient, FrmError
from jawn.sim.host import SimHost

from conftest import PLATE, SWITCH

READS = ["getFactory", "getStorageInv", "getPower", "getSwitches", "getSessionInfo", "getChatMessages", "getProdStats", "getConstructor"]
WRITES = ["setEnabled", "setSwitches", "sendChatMessage"]


@pytest.fixture
async def host(scenario):
    h = await SimHost(scenario, "tok").start()
    yield h
    await h.stop()


def client(h, token="tok", **kw):
    return FrmClient(h.base_url, token, READS, WRITES, timeout_s=kw.pop("timeout_s", 1.0), get_retries=kw.pop("get_retries", 1), **kw)


async def test_read_shapes(host):
    c = client(host)
    fac = await c.get("getFactory")
    m = next(x for x in fac if x["ID"] == PLATE)
    for k in ("ID", "ClassName", "Recipe", "RecipeClassName", "production", "ingredients", "InputInventory", "OutputInventory", "IsProducing", "IsPaused", "PowerInfo", "location", "ManuSpeed"):
        assert k in m
    cons = await c.get("getConstructor")
    assert {x["ClassName"] for x in cons} == {"Build_ConstructorMk1_C"}
    power = await c.get("getPower")
    assert {"CircuitGroupID", "PowerCapacity", "PowerConsumed", "FuseTriggered"} <= set(power[0])
    sess = await c.get("getSessionInfo")
    assert "TotalPlayDuration" in sess
    await c.close()


async def test_api_prefix_and_cors(host):
    import aiohttp

    async with aiohttp.ClientSession() as s:
        async with s.get(host.base_url + "/api/getPower") as r:
            assert r.status == 200
            assert r.headers["Access-Control-Allow-Origin"] == "*"


async def test_writes_require_token(host):
    bad = client(host, token="wrong")
    with pytest.raises(FrmError) as e:
        await bad.post("setEnabled", {"ID": PLATE, "status": False})
    assert e.value.kind == "auth"
    assert host.world.machines[PLATE].paused is False
    none = client(host, token="")
    with pytest.raises(FrmError) as e2:
        await none.post("setEnabled", {"ID": PLATE, "status": False})
    assert e2.value.kind == "auth" and none.stats.posts == 0  # refused before any I/O
    good = client(host)
    await good.post("setEnabled", {"ID": PLATE, "status": False})
    assert host.world.machines[PLATE].paused is True
    await good.post("setSwitches", {"ID": SWITCH, "status": False})
    assert host.world.switches[SWITCH].on is False
    for c in (bad, none, good):
        await c.close()


async def test_unknown_id_reported(host):
    c = client(host)
    res = await c.post("setEnabled", {"ID": "Build_Nope_C_1", "status": True})
    assert res[0].get("Error")
    await c.close()


async def test_allowlist_blocks_before_io(host):
    c = client(host)
    with pytest.raises(FrmError) as e:
        await c.get("getTrainRails")  # known crasher upstream; not allowlisted
    assert e.value.kind == "not_allowed" and c.stats.gets == 0
    with pytest.raises(FrmError):
        await c.post("setRecipe", {})
    await c.close()


def test_refuses_non_loopback():
    with pytest.raises(ValueError):
        FrmClient("http://192.168.1.20:8080", "t", READS, WRITES)
    FrmClient("http://192.168.1.20:8080", "t", READS, WRITES, allow_remote=True)


def test_token_never_in_repr():
    c = FrmClient("http://127.0.0.1:1", "supersecret", READS, WRITES)
    assert "supersecret" not in repr(c)


async def test_get_retries_transient_but_post_never(host):
    host.faults.set_endpoint("getPower", error_rate=1.0, error_status=503)
    c = client(host, get_retries=2)
    with pytest.raises(FrmError) as e:
        await c.get("getPower")
    assert e.value.kind == "http" and c.stats.gets == 3 and c.stats.retries == 2
    host.faults.set_endpoint("setEnabled", error_rate=1.0, error_status=500)
    with pytest.raises(FrmError):
        await c.post("setEnabled", {"ID": PLATE, "status": False})
    assert c.stats.posts == 1  # no retry of writes
    await c.close()


async def test_timeout_and_malformed(host):
    host.faults.set_endpoint("getFactory", delay_s=0.6)
    c = client(host, timeout_s=0.2, get_retries=0)
    with pytest.raises(FrmError) as e:
        await c.get("getFactory")
    assert e.value.kind == "timeout"
    host.faults.set_endpoint("getFactory", delay_s=0.0, malformed_rate=1.0)
    with pytest.raises(FrmError) as e2:
        await c.get("getFactory")
    assert e2.value.kind == "malformed"
    await c.close()


async def test_not_found_all_and_drop(host):
    host.faults.apply({"not_found_all": True})
    c = client(host, get_retries=0)
    with pytest.raises(FrmError) as e:
        await c.get("getPower")
    assert e.value.kind == "not_found"
    host.faults.apply({"clear": True, "endpoints": {"getPower": {"drop_rate": 1.0}}})
    with pytest.raises(FrmError) as e2:
        await c.get("getPower")
    assert e2.value.kind in ("connection", "malformed", "http")
    await c.close()


async def test_frozen_and_omitted(host):
    c = client(host)
    host.faults.set_endpoint("getSessionInfo", frozen=True)
    a = await c.get("getSessionInfo")
    host.runtime.advance(30)
    b = await c.get("getSessionInfo")
    assert a == b
    host.faults.set_endpoint("getSessionInfo", frozen=False)
    assert (await c.get("getSessionInfo"))["TotalPlayDuration"] >= 30
    host.faults.set_endpoint("getFactory", omit_ids=[PLATE])
    assert PLATE not in {m["ID"] for m in await c.get("getFactory")}
    await c.close()


async def test_write_faults(host):
    c = client(host)
    host.faults.set_writes(ignore=True)
    await c.post("setEnabled", {"ID": PLATE, "status": False})
    assert host.world.machines[PLATE].paused is False  # 200 OK but nothing happened
    host.faults.set_writes(ignore=False, apply_delay_s=5)
    await c.post("setEnabled", {"ID": PLATE, "status": False})
    assert host.world.machines[PLATE].paused is False
    host.runtime.advance(5.5)
    assert host.world.machines[PLATE].paused is True
    host.faults.set_writes(apply_delay_s=0, reject_status=503)
    with pytest.raises(FrmError):
        await c.post("setEnabled", {"ID": PLATE, "status": True})
    await c.close()


async def test_sim_control_requires_token(host):
    import aiohttp

    async with aiohttp.ClientSession() as s:
        async with s.get(host.base_url + "/sim/truth") as r:
            assert r.status == 401
        async with s.post(host.base_url + "/sim/human", json={"action": "toggle", "id": PLATE}, headers={"X-Sim-Token": "tok"}) as r:
            assert r.status == 200
    assert host.world.machines[PLATE].paused is True


def test_simhost_refuses_non_loopback(scenario):
    with pytest.raises(ValueError):
        SimHost(scenario, "t", host="0.0.0.0")
