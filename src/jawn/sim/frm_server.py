"""Simulated Ficsit Remote Monitoring (FRM) HTTP server.

Response shapes follow the FRM 1.5.x documentation (getFactory, getStorageInv,
getPower, getSwitches, getSessionInfo, getChatMessages, setEnabled, setSwitches,
sendChatMessage). Exact live shapes are an open P1 item; the harness parser is
deliberately tolerant and P1 will capture real fixtures to reconcile.

Behaviours mirrored from FRM:
  * reads are unauthenticated; writes need ``X-FRM-Authorization: <token>`` (401 otherwise)
  * paths available as ``/getX`` and ``/api/getX``
  * ``Access-Control-Allow-Origin: *`` on every response

Simulator-only routes live under ``/sim/*`` (truth, human actions, faults) and need
``X-Sim-Token``. They do not exist on real FRM.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from aiohttp import web

from .catalog import item
from .faults import FaultInjector
from .world import Machine, World

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

MACHINE_ENDPOINTS: dict[str, tuple[str, ...] | None] = {
    "getFactory": None,  # all manufacturers
    "getConstructor": ("Build_ConstructorMk1_C",),
    "getAssembler": ("Build_AssemblerMk1_C",),
    "getSmelter": ("Build_SmelterMk1_C",),
    "getManufacturer": ("Build_ManufacturerMk1_C",),
}


@dataclass
class SimRuntime:
    """World + fault state + delayed writes. ``advance(dt)`` is the simulation tick."""

    world: World
    faults: FaultInjector = field(default_factory=FaultInjector)
    pending_writes: list[tuple[float, Callable[[], None]]] = field(default_factory=list)
    write_log: list[dict[str, Any]] = field(default_factory=list)

    def advance(self, dt: float) -> None:
        self.world.step(dt)
        if self.pending_writes:
            due = [p for p in self.pending_writes if p[0] <= self.world.game_time + 1e-9]
            self.pending_writes = [p for p in self.pending_writes if p[0] > self.world.game_time + 1e-9]
            for _, fn in due:
                fn()


# --------------------------------------------------------------------------- serializers
def _loc(xyz: tuple[float, float, float]) -> dict[str, float]:
    return {"x": xyz[0], "y": xyz[1], "z": xyz[2], "rotation": 0.0}


def machine_json(w: World, m: Machine) -> dict[str, Any]:
    r = m.recipe
    grid = w.grid_of(m.circuit)
    prod, ingr = [], []
    if r is not None:
        for c, n in r.outputs.items():
            mx = n * 60.0 / m.duration
            prod.append({"Name": item(c).name, "ClassName": c, "Amount": m.outputs.count(c), "CurrentProd": round(mx * m.productivity, 3), "MaxProd": round(mx, 3), "ProdPercent": round(m.productivity * 100, 2)})
        for c, n in r.inputs.items():
            mx = n * 60.0 / m.duration
            ingr.append({"Name": item(c).name, "ClassName": c, "Amount": m.inputs.count(c), "CurrentConsumed": round(mx * m.productivity, 3), "MaxConsumed": round(mx, 3), "ConsPercent": round(m.productivity * 100, 2)})
    return {
        "ID": m.id,
        "Name": m.name,
        "ClassName": m.class_name,
        "location": _loc(m.location),
        "Recipe": r.name if r else "",
        "RecipeClassName": r.class_name if r else "",
        "production": prod,
        "ingredients": ingr,
        "InputInventory": m.inputs.snapshot(),
        "OutputInventory": m.outputs.snapshot(),
        "Productivity": round(m.productivity * 100, 2),
        "ManuSpeed": round(m.clock * 100, 2),
        "Somersloops": 0,
        "PowerShards": 0,
        "IsConfigured": r is not None,
        "IsProducing": m.producing,
        "IsPaused": m.paused,
        "CircuitGroupID": grid.id,
        "PowerInfo": {"CircuitGroupID": grid.id, "CircuitID": grid.id, "PowerConsumed": m.power_mw if m.producing else 0.0, "MaxPowerConsumed": m.power_mw},
        "features": {"properties": {"name": m.name, "type": m.name}, "geometry": {"coordinates": {"x": m.location[0], "y": m.location[1], "z": m.location[2]}, "type": "Point"}},
    }


def power_json(w: World) -> list[dict[str, Any]]:
    out = []
    for g in w.grids:
        consumed = sum(m.power_mw for m in w.machines.values() if m.circuit in g.circuits and m.producing)
        out.append(
            {
                "CircuitGroupID": g.id,
                "CircuitID": g.id,
                "PowerProduction": round(consumed, 3),
                "PowerConsumed": round(consumed, 3),
                "PowerCapacity": round(g.capacity, 3),
                "PowerMaxConsumed": round(g.max_consumed, 3),
                "BatteryDifferential": 0.0,
                "BatteryPercent": 0.0,
                "BatteryCapacity": 0.0,
                "BatteryTimeEmpty": "00:00:00",
                "BatteryTimeFull": "00:00:00",
                "FuseTriggered": g.fuse,
            }
        )
    return out


def session_json(w: World) -> dict[str, Any]:
    t = int(w.game_time)
    return {
        "SessionName": w.session_name,
        "IsPaused": w.game_paused,
        "DayLength": 50,
        "NightLength": 10,
        "PassedDays": t // 3600,
        "NumberOfDaysSinceLastDeath": t // 3600,
        "Hours": (t // 3600) % 24,
        "Minutes": (t // 60) % 60,
        "Seconds": t % 60,
        "IsDay": True,
        "TotalPlayDuration": t,
        "TotalPlayDurationText": f"{t // 3600:02d}:{(t // 60) % 60:02d}:{t % 60:02d}",
    }


def prodstats_json(w: World) -> list[dict[str, Any]]:
    agg: dict[str, dict[str, float]] = {}
    for m in w.machines.values():
        if not m.recipe:
            continue
        for c, n in m.recipe.outputs.items():
            a = agg.setdefault(c, {"CurrentProd": 0.0, "MaxProd": 0.0, "CurrentConsumed": 0.0, "MaxConsumed": 0.0})
            mx = n * 60.0 / m.duration
            a["MaxProd"] += mx
            a["CurrentProd"] += mx * m.productivity
        for c, n in m.recipe.inputs.items():
            a = agg.setdefault(c, {"CurrentProd": 0.0, "MaxProd": 0.0, "CurrentConsumed": 0.0, "MaxConsumed": 0.0})
            mx = n * 60.0 / m.duration
            a["MaxConsumed"] += mx
            a["CurrentConsumed"] += mx * m.productivity
    return [{"Name": item(c).name, "ClassName": c, **{k: round(v, 3) for k, v in a.items()}} for c, a in sorted(agg.items())]


def snapshot_world(w: World, endpoint: str) -> Any:
    if endpoint in MACHINE_ENDPOINTS:
        classes = MACHINE_ENDPOINTS[endpoint]
        return [machine_json(w, m) for m in w.machines.values() if classes is None or m.class_name in classes]
    if endpoint == "getStorageInv":
        return [{"ID": s.id, "Name": s.name, "ClassName": s.class_name, "location": _loc(s.location), "Inventory": s.inventory.snapshot()} for s in w.storages.values()]
    if endpoint == "getWorldInv":
        tot: dict[str, int] = {}
        for s in w.storages.values():
            for c, n in s.inventory.items.items():
                tot[c] = tot.get(c, 0) + n
        return [{"Name": item(c).name, "ClassName": c, "Amount": n, "MaxAmount": item(c).stack} for c, n in sorted(tot.items())]
    if endpoint == "getPower":
        return power_json(w)
    if endpoint == "getSwitches":
        return [{"ID": s.id, "Name": s.name, "ClassName": s.class_name, "SwitchTag": s.name, "IsOn": s.on, "Priority": s.priority, "Connected0": s.a, "Connected1": s.b} for s in w.switches.values()]
    if endpoint == "getGenerators":
        return [{"ID": g.id, "Name": g.name, "ClassName": g.class_name, "PowerProductionCapacity": g.capacity_mw if g.online else 0.0, "IsFullSpeed": g.online, "IsPaused": not g.online, "CircuitGroupID": w.grid_of(g.circuit).id} for g in w.generators.values()]
    if endpoint == "getSessionInfo":
        return session_json(w)
    if endpoint == "getChatMessages":
        return list(w.chat[-50:])
    if endpoint == "getProdStats":
        return prodstats_json(w)
    raise KeyError(endpoint)


READ_ENDPOINTS = list(MACHINE_ENDPOINTS) + ["getStorageInv", "getWorldInv", "getPower", "getSwitches", "getGenerators", "getSessionInfo", "getChatMessages", "getProdStats"]


# --------------------------------------------------------------------------- app
def _cors(resp: web.StreamResponse) -> web.StreamResponse:
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-FRM-Authorization"
    return resp


def make_app(rt: SimRuntime, token: str, sim_token: str | None = None) -> web.Application:
    sim_token = sim_token or token
    app = web.Application()
    w = rt.world
    fi = rt.faults

    async def common_faults(request: web.Request, name: str) -> web.StreamResponse | None:
        st = fi.state
        if st.server_down:
            request.transport.close() if request.transport else None  # type: ignore[func-returns-value]
            raise ConnectionResetError("sim server down")
        if st.not_found_all:
            return _cors(web.json_response({"error": "No matching endpoint found."}, status=404))
        f = fi.for_endpoint(name)
        if f.delay_s > 0:
            await asyncio.sleep(f.delay_s)
        if fi.roll(f.drop_rate):
            if request.transport:
                request.transport.close()
            raise ConnectionResetError("sim dropped connection")
        if fi.roll(f.error_rate):
            return _cors(web.json_response({"error": "simulated failure"}, status=f.error_status))
        return None

    def read_handler(name: str) -> Handler:
        async def h(request: web.Request) -> web.StreamResponse:
            early = await common_faults(request, name)
            if early is not None:
                return early
            f = fi.for_endpoint(name)
            if f.frozen:
                if name not in fi.frozen_cache:
                    fi.frozen_cache[name] = snapshot_world(w, name)
                data = fi.frozen_cache[name]
            else:
                data = snapshot_world(w, name)
            if f.omit_ids and isinstance(data, list):
                data = [d for d in data if d.get("ID") not in f.omit_ids]
            if fi.roll(f.malformed_rate):
                return _cors(web.Response(text='[{"ID": "Build_', content_type="application/json"))
            return _cors(web.json_response(data))

        return h

    def authorized(request: web.Request) -> bool:
        return request.headers.get("X-FRM-Authorization", "") == token and bool(token)

    async def parse_body(request: web.Request) -> list[dict[str, Any]]:
        body = await request.json()
        if isinstance(body, dict):
            return [body]
        if isinstance(body, list) and all(isinstance(b, dict) for b in body):
            return body
        raise ValueError("body must be an object or array of objects")

    def write_handler(name: str, apply: Callable[[dict[str, Any]], dict[str, Any]]) -> Handler:
        async def h(request: web.Request) -> web.StreamResponse:
            early = await common_faults(request, name)
            if early is not None:
                return early
            if not authorized(request):
                return _cors(web.json_response({"error": "Unauthorized"}, status=401))
            try:
                items = await parse_body(request)
            except (ValueError, json.JSONDecodeError) as e:
                return _cors(web.json_response({"error": f"bad request: {e}"}, status=400))
            wf = fi.state.writes
            if wf.reject_status:
                return _cors(web.json_response({"error": "simulated write rejection"}, status=wf.reject_status))
            results = []
            for it in items:
                rt.write_log.append({"t": w.game_time, "endpoint": name, "body": it, "ignored": wf.ignore, "delay": wf.apply_delay_s})
                try:
                    preview = apply(dict(it, _dry=True))
                except KeyError as e:
                    results.append({"ID": it.get("ID"), "Error": f"unknown ID {e}"})
                    continue
                if not wf.ignore:
                    if wf.apply_delay_s > 0:
                        rt.pending_writes.append((w.game_time + wf.apply_delay_s, lambda it=it: apply(it)))
                    else:
                        apply(it)
                results.append(preview)
            return _cors(web.json_response(results))

        return h

    def apply_enabled(it: dict[str, Any]) -> dict[str, Any]:
        oid = str(it.get("ID", ""))
        status = bool(it.get("status", True))
        if oid in w.machines:
            if not it.get("_dry"):
                w.set_paused(oid, not status, by="frm")
            return {"ID": oid, "Name": w.machines[oid].name, "Status": status}
        if oid in w.switches:
            if not it.get("_dry"):
                w.set_switch(oid, on=status, by="frm")
            return {"ID": oid, "Name": w.switches[oid].name, "Status": status}
        raise KeyError(oid)

    def apply_switch(it: dict[str, Any]) -> dict[str, Any]:
        oid = str(it.get("ID", ""))
        if oid not in w.switches:
            raise KeyError(oid)
        if not it.get("_dry"):
            w.set_switch(oid, on=it.get("status"), priority=it.get("priority"), name=it.get("name"), by="frm")
        s = w.switches[oid]
        return {"ID": oid, "Name": it.get("name", s.name), "Status": it.get("status", s.on), "Priority": it.get("priority", s.priority)}

    def apply_chat(it: dict[str, Any]) -> dict[str, Any]:
        msg = str(it.get("message", ""))
        if not it.get("_dry"):
            w.add_chat(str(it.get("sender", "FRM")), msg, kind="System")
        return {"IsSent": True, "Message": msg}

    for name in READ_ENDPOINTS:
        h = read_handler(name)
        app.router.add_get(f"/{name}", h)
        app.router.add_get(f"/api/{name}", h)
    for name, fn in (("setEnabled", apply_enabled), ("setSwitches", apply_switch), ("sendChatMessage", apply_chat)):
        h = write_handler(name, fn)
        app.router.add_post(f"/{name}", h)
        app.router.add_post(f"/api/{name}", h)

    async def options(request: web.Request) -> web.StreamResponse:
        return _cors(web.Response(status=204))

    app.router.add_route("OPTIONS", "/{tail:.*}", options)

    # ---------------- simulator-only control plane
    def sim_auth(request: web.Request) -> None:
        if request.headers.get("X-Sim-Token", "") != sim_token:
            raise web.HTTPUnauthorized(text="sim token required")

    async def sim_truth(request: web.Request) -> web.StreamResponse:
        sim_auth(request)
        return web.json_response(truth_json(rt))

    async def sim_human(request: web.Request) -> web.StreamResponse:
        sim_auth(request)
        spec = await request.json()
        try:
            res = human_action(rt, spec)
        except (KeyError, ValueError) as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)
        return web.json_response({"ok": True, **res})

    async def sim_faults(request: web.Request) -> web.StreamResponse:
        sim_auth(request)
        if request.method == "POST":
            try:
                fi.apply(await request.json())
            except (KeyError, TypeError) as e:
                return web.json_response({"ok": False, "error": str(e)}, status=400)
        return web.json_response(fi.describe())

    app.router.add_get("/sim/truth", sim_truth)
    app.router.add_post("/sim/human", sim_human)
    app.router.add_get("/sim/faults", sim_faults)
    app.router.add_post("/sim/faults", sim_faults)
    return app


def truth_json(rt: SimRuntime) -> dict[str, Any]:
    w = rt.world
    return {
        "game_time": w.game_time,
        "game_paused": w.game_paused,
        "machines": {m.id: {"paused": m.paused, "producing": m.producing, "status": m.status, "produced": m.produced, "consumed": m.consumed, "circuit": m.circuit} for m in w.machines.values()},
        "storages": {s.id: dict(s.inventory.items) for s in w.storages.values()},
        "fused_circuits": sorted(w.fused),
        "switches": {s.id: {"on": s.on, "priority": s.priority} for s in w.switches.values()},
        "generators": {g.id: {"online": g.online, "capacity_mw": g.capacity_mw} for g in w.generators.values()},
        "timeline": w.timeline,
        "events": w.events,
        "write_log": rt.write_log,
    }


def human_action(rt: SimRuntime, spec: dict[str, Any]) -> dict[str, Any]:
    """Simulated pioneer (Dave) actions. These bypass FRM entirely, like a player would."""
    w = rt.world
    a = spec.get("action")
    if a == "set_paused":
        w.set_paused(spec["id"], bool(spec["paused"]), by="human")
    elif a == "toggle":
        w.set_paused(spec["id"], not w.machines[spec["id"]].paused, by="human")
    elif a == "reset_fuse":
        w.reset_fuse(spec.get("circuit"), by="human")
    elif a == "switch":
        w.set_switch(spec["id"], on=bool(spec["on"]), by="human")
    elif a == "chat":
        w.add_chat(spec.get("sender", "Dave"), str(spec["message"]))
    elif a == "pause_game":
        w.game_paused = True
    elif a == "resume_game":
        w.game_paused = False
    elif a == "take_items":
        taken = w.storages[spec["id"]].inventory.take(spec["item"], int(spec["count"]))
        return {"taken": taken}
    elif a == "add_items":
        added = w.storages[spec["id"]].inventory.add(spec["item"], int(spec["count"]))
        return {"added": added}
    elif a == "generator":
        g = w.generators[spec["id"]]
        if "online" in spec:
            g.online = bool(spec["online"])
        if "capacity_mw" in spec:
            g.capacity_mw = float(spec["capacity_mw"])
        w._recompute_grids()
    elif a == "supply":
        s = w.supplies[spec["id"]]
        if "enabled" in spec:
            s.enabled = bool(spec["enabled"])
        if "rate_per_min" in spec:
            s.rate_per_min = float(spec["rate_per_min"])
    elif a == "reload":
        return {"mapping": w.reload(rename_ids=bool(spec.get("rename_ids", False)))}
    else:
        raise ValueError(f"unknown human action {a!r}")
    w._event("human", **{k: v for k, v in spec.items()})
    return {}
