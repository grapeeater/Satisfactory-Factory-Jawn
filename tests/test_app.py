"""Dashboard, realtime mode, CLI and config validation."""

import asyncio
import json

import aiohttp
import pytest
from pydantic import ValidationError

from jawn.app import build_session
from jawn.cli import main as cli_main
from jawn.config import Config
from jawn.dashboard.server import start_dashboard

from conftest import PLATE, make_cfg


async def test_dashboard_state_and_token(cfg, scenario):
    sess = await build_session(cfg, controller="agents", sim_scenario=scenario)
    dash, token, port = await start_dashboard(sess, "127.0.0.1", 0)
    try:
        await sess.run()
        base = f"http://127.0.0.1:{port}"
        async with aiohttp.ClientSession() as s:
            async with s.get(base + "/") as r:
                html = await r.text()
                assert r.status == 200 and token in html and "EMERGENCY STOP" in html
            async with s.get(base + "/api/state") as r:
                st = await r.json()
            for k in ("run", "machines", "production", "series", "agents", "actions", "conflicts", "issues", "alerts", "budget", "power", "timeline"):
                assert k in st
            assert {a for a in st["agents"]} == {"plate_agent", "rod_agent"}
            assert any(m["alias"] == "plate" and m["managed"] for m in st["machines"])
            assert any(not m["managed"] for m in st["machines"]), "Dave's smelter shown as not managed"
            assert st["actions"] and st["actions"][0]["stages"]
            async with s.get(base + "/api/events?after=0") as r:
                assert (await r.json())[0]["type"] == "run_start"
            async with s.post(base + "/api/stop", json={"mode": "hold"}) as r:
                assert r.status == 403
            async with s.post(base + "/api/sim/human", json={"action": "toggle", "id": PLATE}) as r:
                assert r.status == 403
            async with s.post(base + "/api/sim/human", json={"action": "toggle", "id": PLATE}, headers={"X-Jawn-Token": token}) as r:
                assert (await r.json())["ok"]
    finally:
        await dash.cleanup()


async def test_dashboard_refuses_non_loopback(cfg, scenario):
    sess = await build_session(cfg, controller="baseline", sim_scenario=scenario)
    with pytest.raises(ValueError):
        await start_dashboard(sess, "0.0.0.0", 0)
    await sess.sim.stop()


async def test_realtime_mode_with_dashboard_stop(cfg, scenario):
    """Wall-clock path: sim ticking in the background, concurrent model tasks, HTTP stop."""
    cfg.experiment.duration_s = 1200
    sess = await build_session(cfg, controller="agents", sim_scenario=scenario, lockstep=False, speed=40.0)
    dash, token, port = await start_dashboard(sess, "127.0.0.1", 0)
    task = asyncio.create_task(sess.run())
    try:
        base = f"http://127.0.0.1:{port}"
        async with aiohttp.ClientSession() as s:
            for _ in range(200):
                await asyncio.sleep(0.05)
                async with s.get(base + "/api/state") as r:
                    st = await r.json()
                if st["run"]["status"] == "running" and st["run"]["elapsed"] > 60:
                    break
            assert st["run"]["status"] == "running"
            async with s.post(base + "/api/stop", json={"mode": "restore"}, headers={"X-Jawn-Token": token}) as r:
                assert (await r.json())["mode"] == "restore"
        res = await asyncio.wait_for(task, timeout=30)
        assert res.status == "stopped" and res.report["stop"][0]["source"] == "dashboard"
        assert res.report["restore"]["complete"]
        w = sess.sim.world
        assert not w.machines[PLATE].paused
    finally:
        await dash.cleanup()


def test_cli_run_and_report(tmp_path, capsys):
    rc = cli_main(["run", "--controller", "baseline", "--out", str(tmp_path), "--duration", "600"])
    out = capsys.readouterr().out
    assert rc == 0 and '"status": "done"' in out
    run_dir = next(p for p in tmp_path.iterdir() if p.is_dir())
    rep = json.loads((run_dir / "report.json").read_text())
    assert rep["summary"]["targets_met_within_duration"]
    assert cli_main(["report", str(run_dir)]) == 0
    assert "Production" in capsys.readouterr().out


async def test_capture_against_simulator(scenario, tmp_path):
    """The P1 capture tool, pointed at the simulated FRM (read-only)."""
    import argparse

    from jawn.cli import _capture
    from jawn.sim.host import SimHost

    h = await SimHost(scenario, "tok").start()
    try:
        args = argparse.Namespace(config="experiments/plates_and_rods.toml", url=h.base_url, endpoints=None, timeout=2.0, gap=0.0, out=str(tmp_path))
        assert await _capture(args) == 0
    finally:
        await h.stop()
    d = next(tmp_path.iterdir())
    assert (d / "getFactory.json").exists() and (d / "getPower.json").exists()
    assert h.runtime.write_log == []


def test_cli_refuses_live_writes_without_approval(monkeypatch, capsys):
    monkeypatch.delenv("JAWN_ALLOW_LIVE_WRITES", raising=False)
    assert cli_main(["run", "--live"]) == 2
    assert "refusing live writes" in capsys.readouterr().err


def test_config_validation():
    base = make_cfg().model_dump()
    bad = json.loads(json.dumps(base))
    bad["agents"][1]["owns"] = ["plate"]
    with pytest.raises(ValidationError, match="owned by both"):
        Config.model_validate(bad)
    bad2 = json.loads(json.dumps(base))
    bad2["constraints"][0]["targets"] = ["plate", "ghost"]
    with pytest.raises(ValidationError, match="unknown target"):
        Config.model_validate(bad2)
    bad3 = json.loads(json.dumps(base))
    bad3["safety"]["typo_field"] = 1
    with pytest.raises(ValidationError):
        Config.model_validate(bad3)


async def test_paid_provider_refused_in_lockstep(cfg, scenario, monkeypatch):
    from jawn.llm.deepseek import DeepSeekProvider

    monkeypatch.setenv("JAWN_ALLOW_PAID_CALLS", "1")
    cfg.llm.deepseek.allow_paid = True
    prov = DeepSeekProvider(cfg.llm.deepseek, api_key="unused")
    with pytest.raises(ValueError, match="wall-clock"):
        await build_session(cfg, provider=prov, sim_scenario=scenario)
    await prov.close()


def test_compare_refuses_real_provider_without_realtime(monkeypatch, capsys):
    monkeypatch.delenv("JAWN_ALLOW_PAID_CALLS", raising=False)
    assert cli_main(["compare", "--config", "experiments/sim_deepseek_plates_and_rods.toml", "--provider", "deepseek", "--runs", "1"]) == 2
    assert "--realtime" in capsys.readouterr().err


def test_shipped_config_cannot_spend():
    cfg = make_cfg()
    assert cfg.llm.deepseek.model == "deepseek-flash"
    assert cfg.llm.deepseek.allow_paid is False and cfg.budget.max_usd == 0.0
    assert cfg.frm.base_url.startswith("http://127.0.0.1")
