"""Local dashboard: agent decisions, machine states, production, conflicts, errors, API usage.

Loopback only. Mutating endpoints (stop / hold / resume / simulated human actions / faults)
require the per-process token embedded in the served page (blocks drive-by requests from
other local web pages).
"""

from __future__ import annotations

import secrets
from importlib import resources
from typing import TYPE_CHECKING, Any

from aiohttp import web

from ..context import item_name
from ..sim.frm_server import human_action

if TYPE_CHECKING:
    from ..app import Session

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def dashboard_state(sess: "Session") -> dict[str, Any]:
    r = sess.runner
    now = r.clock.now()
    snap = r.snap
    owners = {t: a.id for a in r.cfg.agents for t in a.owns}
    machines = []
    managed_ids = set()
    if snap is not None:
        for alias, b in r.cfg.targets.items():
            oid = r.st.resolved.get(alias, b.id)
            managed_ids.add(oid)
            m = snap.machines.get(oid) if b.kind == "machine" else None
            lk = r.st.locked(alias, now)
            entry: dict[str, Any] = {"alias": alias, "id": oid, "owner": owners.get(alias, "-"), "managed": True, "pending": alias in r.st.pending, "lock_s": round(lk.until - now) if lk else 0, "suspect": r.st.is_suspect(alias, now), "initial": r.st.initial.get(alias)}
            if m is not None:
                entry.update(_mach(m))
            else:
                entry["state"] = "missing"
            machines.append(entry)
        for m in snap.machines.values():
            if m.id not in managed_ids:
                machines.append({"alias": m.name, "id": m.id, "owner": "(not managed)", "managed": False, **_mach(m)})
    t0 = r.main_started or 0.0
    production = [
        {"item": it, "name": item_name(it), "delivered": r.outcome.total(it), "target": n, "reached_after_s": round(r.outcome.reached_at[it] - t0, 1) if it in r.outcome.reached_at else None}
        for it, n in r.targets_all.items()
    ]
    series = [[round(t - t0, 1), v] for t, v in r.outcome.series[-400:]]
    ring = list(r.log.ring)
    issues = [e for e in ring if e.get("level") in ("warn", "error")][-60:]
    ctrl = r.controller.summary()
    return {
        "run": {
            "id": r.run_id,
            "experiment": r.cfg.experiment.name,
            "controller": r.controller.name,
            "provider": ctrl.get("provider"),
            "status": r.status,
            "t": round(now, 1),
            "elapsed": round(now - t0, 1) if r.main_started is not None else 0,
            "duration": r.cfg.experiment.duration_s,
            "health": snap.health.value if snap else "unknown",
            "game_time": snap.game_time if snap else None,
            "held": r.st.held,
            "stopped": r.stop.stopped,
            "stop": vars(r.stop.request) if r.stop.request else None,
            "sim": sess.sim is not None,
            "constraints": [{"name": c.name, "targets": c.targets, "max": c.max, "running": [a for a in c.targets if snap is not None and r.st.observe(snap, a) is False]} for c in r.cfg.constraints],
            "max_running": r.max_running,
        },
        "machines": machines,
        "power": [vars(c) | {"headroom": round(c.headroom, 2)} for c in (snap.circuits.values() if snap else [])],
        "switches": [vars(s) for s in (snap.switches.values() if snap else [])],
        "production": production,
        "series": series,
        "agents": ctrl.get("agents", {}),
        "actions": list(reversed(list(r.actions.values())[-80:])),
        "conflicts": [e for e in ring if e["type"] == "conflict"][-30:][::-1],
        "overrides": [e for e in ring if e["type"] in ("human_override", "constraint_violation")][-30:][::-1],
        "issues": issues[::-1],
        "alerts": r.alerts[-40:][::-1],
        "messages": [e for e in ring if e["type"] == "message"][-30:][::-1],
        "budget": r.budget.summary(),
        "timeline": r.machine_timeline[-300:],
        "restore": r.restore_result,
        "frm_client": vars(r.client.stats),
        "sim_generators": sorted(sess.sim.world.generators) if sess.sim is not None else [],
    }


def _mach(m: Any) -> dict[str, Any]:
    return {
        "state": m.condition(),
        "paused": m.paused,
        "producing": m.producing,
        "recipe": m.recipe,
        "inputs": {item_name(k): v for k, v in m.inputs.items()},
        "outputs": {item_name(k): v for k, v in m.outputs.items()},
        "productivity": m.productivity,
        "circuit": m.circuit_id,
        "power_mw": m.power_mw,
    }


def make_dashboard(sess: "Session", token: str | None = None) -> tuple[web.Application, str]:
    token = token or secrets.token_urlsafe(18)
    app = web.Application()
    html = resources.files("jawn.dashboard").joinpath("index.html").read_text(encoding="utf-8").replace("__JAWN_TOKEN__", token)

    def check(request: web.Request) -> None:
        if request.headers.get("X-Jawn-Token") != token:
            raise web.HTTPForbidden(text="missing or wrong dashboard token")

    async def index(request: web.Request) -> web.StreamResponse:
        return web.Response(text=html, content_type="text/html")

    async def state(request: web.Request) -> web.StreamResponse:
        return web.json_response(dashboard_state(sess), dumps=_dumps)

    async def events(request: web.Request) -> web.StreamResponse:
        after = int(request.query.get("after", "0"))
        return web.json_response(sess.runner.log.since(after, 300), dumps=_dumps)

    async def stop(request: web.Request) -> web.StreamResponse:
        check(request)
        body = await _json(request)
        mode = body.get("mode") if body.get("mode") in ("hold", "restore") else None
        req = sess.runner.stop.trigger("dashboard emergency stop", "dashboard", mode)
        return web.json_response({"ok": True, "mode": req.mode})

    async def hold(request: web.Request) -> web.StreamResponse:
        check(request)
        sess.runner.st.held = True
        sess.runner.alert("warn", "writes on hold (dashboard)")
        return web.json_response({"ok": True})

    async def resume(request: web.Request) -> web.StreamResponse:
        check(request)
        sess.runner.st.held = False
        sess.runner.alert("info", "writes resumed (dashboard)")
        return web.json_response({"ok": True})

    async def sim_human(request: web.Request) -> web.StreamResponse:
        check(request)
        if sess.sim is None:
            raise web.HTTPBadRequest(text="not running on the simulator")
        try:
            return web.json_response({"ok": True, **human_action(sess.sim.runtime, await _json(request))})
        except (KeyError, ValueError) as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)

    async def sim_faults(request: web.Request) -> web.StreamResponse:
        check(request)
        if sess.sim is None:
            raise web.HTTPBadRequest(text="not running on the simulator")
        try:
            sess.sim.faults.apply(await _json(request))
        except (KeyError, TypeError) as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)
        return web.json_response({"ok": True, "faults": sess.sim.faults.describe()})

    app.router.add_get("/", index)
    app.router.add_get("/api/state", state)
    app.router.add_get("/api/events", events)
    app.router.add_post("/api/stop", stop)
    app.router.add_post("/api/hold", hold)
    app.router.add_post("/api/resume", resume)
    app.router.add_post("/api/sim/human", sim_human)
    app.router.add_post("/api/sim/faults", sim_faults)
    return app, token


async def _json(request: web.Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return {}
    return body if isinstance(body, dict) else {}


def _dumps(obj: Any) -> str:
    import json

    return json.dumps(obj, default=lambda o: getattr(o, "value", str(o)))


async def start_dashboard(sess: "Session", host: str = "127.0.0.1", port: int = 8787, token: str | None = None) -> tuple[web.AppRunner, str, int]:
    if host not in LOOPBACK:
        raise ValueError("the dashboard only binds to loopback")
    app, token = make_dashboard(sess, token)
    runner = web.AppRunner(app, access_log=None, handle_signals=False)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    if port == 0:
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    return runner, token, port
