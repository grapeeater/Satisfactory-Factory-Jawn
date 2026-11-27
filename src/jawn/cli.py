"""Command line: jawn run | compare | sim-serve | report | deepseek-models"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import random
import sys
import time
from pathlib import Path
from typing import Any

from .app import build_session
from .config import Config, load_config
from .sim.host import SimHost, load_scenario

ROOT = Path.cwd()


def _run_dir(base: str, name: str) -> tuple[Path, str]:
    run_id = time.strftime("%Y%m%d-%H%M%S") + f"-{name}"
    d = Path(base) / run_id
    d.mkdir(parents=True, exist_ok=True)
    return d, run_id


def perturb(scn: dict[str, Any], seed: int) -> dict[str, Any]:
    """Seeded variation of starting conditions for matched runs (same seed -> same factory)."""
    if seed == 0:
        return scn
    rng = random.Random(seed)
    s = copy.deepcopy(scn)
    # stock tight enough that starvation can happen (200 ingots are needed for 80+80)
    for st in s.get("storages", []):
        for k, v in list(st.get("items", {}).items()):
            st["items"][k] = max(0, int(v * rng.uniform(0.0, 1.1)))
    for sp in s.get("supplies", []):
        sp["rate_per_min"] = round(sp["rate_per_min"] * rng.uniform(0.3, 1.2), 2)
    for m in s.get("machines", []):
        if m.get("class_name") == "Build_ConstructorMk1_C":
            m["input_items"] = {"Desc_IronIngot_C": rng.randint(0, 60)}
    return s


async def _run(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if args.provider:
        cfg.llm.provider = args.provider
    if args.duration:
        cfg.experiment.duration_s = args.duration
    live = bool(args.live)
    if live and not args.observe_only and os.environ.get("JAWN_ALLOW_LIVE_WRITES") != "1":
        print("refusing live writes: set JAWN_ALLOW_LIVE_WRITES=1 (P1 approval) or use --observe-only", file=sys.stderr)
        return 2
    scn = None
    if not live:
        scn = perturb(load_scenario(cfg.resolve(cfg.sim.scenario or "")), args.seed)
    run_dir, run_id = _run_dir(args.out, cfg.experiment.name)
    sess = await build_session(cfg, use_sim=not live, lockstep=not (args.realtime or live), speed=args.speed, controller=args.controller, run_dir=run_dir, run_id=run_id, sim_scenario=scn, seed=args.seed, observe_only=args.observe_only)
    dash = None
    if args.dashboard:
        from .dashboard.server import start_dashboard

        dash, _, port = await start_dashboard(sess, cfg.dashboard.host, args.port or cfg.dashboard.port)
        print(f"dashboard: http://{cfg.dashboard.host}:{port}/")
    print(f"run {run_id}: controller={sess.runner.controller.name} provider={cfg.llm.provider if sess.runner.controller.name == 'agents' else '-'} {'LIVE FRM ' + cfg.frm.base_url if live else 'simulator'} {'observe-only' if args.observe_only else ''}")
    res = await sess.run()
    s = res.report["summary"]
    print(json.dumps({k: s[k] for k in ("status", "elapsed_s", "targets_met_within_duration", "verified", "unverified", "conflicts", "harness_constraint_violations", "llm_calls", "real_cost_usd", "simulated_cost_usd")}, indent=1))
    print(f"report: {run_dir / 'report.md'}")
    if dash is not None:
        if args.linger:
            print(f"dashboard stays up for {args.linger:.0f}s (Ctrl-C to exit)")
            try:
                await asyncio.sleep(args.linger)
            except asyncio.CancelledError:
                pass
        await dash.cleanup()
    return 0 if res.status in ("done", "stopped") else 1


async def _compare(args: argparse.Namespace) -> int:
    cfg0 = load_config(args.config)
    if args.provider:
        cfg0.llm.provider = args.provider
    real = cfg0.llm.provider == "deepseek"
    if real and not args.realtime:
        print("a real model provider needs --realtime (wall-clock time)", file=sys.stderr)
        return 2
    base_scn = load_scenario(cfg0.resolve(cfg0.sim.scenario or ""))
    rows = []
    spent = 0.0  # real USD across the whole comparison; each run's cap is clipped to what is left
    seeds = [int(x) for x in args.seeds.split(",")] if args.seeds else list(range(1, args.runs + 1))
    for seed in seeds:
        for ctrl in ("baseline", "agents"):
            cfg = Config.model_validate(cfg0.model_dump())
            cfg.llm.mock.seed = seed
            cfg.llm.mock.latency_s = random.Random(seed).uniform(1.0, 6.0)
            if real and ctrl == "agents":
                left = args.max_total_usd - spent
                if left < 0.005:
                    print(f"stopping: total real spend ${spent:.4f} has reached the comparison cap ${args.max_total_usd:.2f}")
                    break
                cfg.budget.max_usd = min(cfg.budget.max_usd, left)
            run_dir, run_id = _run_dir(args.out, f"{cfg.experiment.name}-{ctrl}-s{seed}")
            sess = await build_session(cfg, controller=ctrl, lockstep=not args.realtime, speed=args.speed, run_dir=run_dir, run_id=run_id, sim_scenario=perturb(base_scn, seed), seed=seed)
            res = await sess.run()
            rep = res.report
            s = rep["summary"]
            spent += s["real_cost_usd"]
            real_led = rep["budget"]["real"]
            row = {"seed": seed, "controller": ctrl, "status": s["status"], "met": s["targets_met_within_duration"]}
            for it, p in rep["production"].items():
                row[p["item"]] = p["delivered"]
                row[p["item"] + " @s"] = p["reached_after_s"]
            row.update({"cmds": s["commands_sent"], "verified": s["verified"], "unverified": s["unverified"], "conflicts": s["conflicts"], "violations": s["harness_constraint_violations"], "calls": s["llm_calls"], "invalid": s["invalid_outputs"], "llm_err": s["llm_errors"]})
            if real:
                row.update({"cost_usd": round(s["real_cost_usd"], 5), "cache_hit": real_led["cache_hit_ratio"], "run": run_id})
            else:
                row["sim_cost"] = s["simulated_cost_usd"]
            rows.append(row)
            print(json.dumps(row))
        else:
            continue
        break
    if real:
        print(f"total real spend: ${spent:.5f} (cap ${args.max_total_usd:.2f})")
    out = Path(args.out) / f"compare-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(rows, indent=1), encoding="utf-8")
    keys = list(rows[0].keys())
    print("\n| " + " | ".join(keys) + " |\n|" + "---|" * len(keys))
    for r in rows:
        print("| " + " | ".join(str(r[k]) for k in keys) + " |")
    print(f"\nsaved {out}")
    return 0 if all(r["met"] and r["violations"] == 0 and r["unverified"] == 0 for r in rows) else 1


async def _sim_serve(args: argparse.Namespace) -> int:
    scn = load_scenario(args.scenario)
    host = await SimHost(scn, args.token, port=args.port, seed=args.seed).start()
    host.start_realtime(args.speed)
    print(f"simulated FRM on {host.base_url} (speed x{args.speed}); writes need X-FRM-Authorization: <token>; Ctrl-C to stop")
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        await host.stop()
    return 0


async def _capture(args: argparse.Namespace) -> int:
    """Read-only: fetch each allowlisted read endpoint once, save raw JSON fixtures and list
    machines (ID, recipe, location, paused) to fill in a live experiment config. P1 tool."""
    from .frm.client import FrmClient, FrmError
    from .observe.snapshot import parse_machines

    cfg = load_config(args.config)
    url = args.url or cfg.frm.base_url
    eps = args.endpoints.split(",") if args.endpoints else cfg.frm.read_endpoints
    c = FrmClient(url, None, eps, [], timeout_s=args.timeout, get_retries=0, allow_remote=False)
    out = Path(args.out) / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    machines: Any = None
    ok = True
    for ep in eps:
        t0 = time.monotonic()
        try:
            data = await c.get(ep)
            ms = (time.monotonic() - t0) * 1000
            (out / f"{ep}.json").write_text(json.dumps(data, indent=1), encoding="utf-8")
            n = len(data) if isinstance(data, list) else 1
            print(f"{ep:18s} OK   {ms:7.1f} ms  {n} objects")
            if ep == "getFactory":
                machines = data
        except FrmError as e:
            ok = False
            print(f"{ep:18s} FAIL {e}")
        await asyncio.sleep(args.gap)
    await c.close()
    if machines is not None:
        errs: list[str] = []
        print("\nmachines (use these for [targets.*] in a live config):")
        for m in parse_machines(machines, errs).values():
            print(f"  {m.id:45s} {m.class_name:28s} {m.recipe_class:28s} paused={m.paused} loc={m.location}")
        if errs:
            print("parser notes:", errs[:10])
    print(f"\nraw fixtures saved to {out}")
    return 0 if ok else 1


async def _deepseek_models(args: argparse.Namespace) -> int:
    """GET /models: free (no tokens billed). Used to verify the model identifier."""
    import aiohttp

    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        print("DEEPSEEK_API_KEY not set", file=sys.stderr)
        return 2
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
        async with s.get("https://api.deepseek.com/models", headers={"Authorization": f"Bearer {key}"}) as r:
            data = await r.json()
    for m in data.get("data", []):
        print(m.get("id"))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="jawn", description="Satisfactory Factory Jawn harness")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run an experiment (simulator by default)")
    r.add_argument("--config", default="experiments/plates_and_rods.toml")
    r.add_argument("--controller", choices=["agents", "baseline"])
    r.add_argument("--provider", choices=["mock", "replay", "deepseek"])
    r.add_argument("--seed", type=int, default=0)
    r.add_argument("--duration", type=float)
    r.add_argument("--realtime", action="store_true", help="wall-clock time (with --speed against the simulator)")
    r.add_argument("--speed", type=float, default=1.0)
    r.add_argument("--dashboard", action="store_true")
    r.add_argument("--port", type=int)
    r.add_argument("--linger", type=float, default=0.0, help="keep the dashboard up this many seconds after the run")
    r.add_argument("--live", action="store_true", help="use the real FRM at frm.base_url (P1+, needs approval)")
    r.add_argument("--observe-only", action="store_true", help="never write; observe and log only")
    r.add_argument("--out", default="runs")
    c = sub.add_parser("compare", help="matched runs: baseline vs agents over N seeds (simulator, lockstep)")
    c.add_argument("--config", default="experiments/plates_and_rods.toml")
    c.add_argument("--runs", type=int, default=3)
    c.add_argument("--seeds", help="comma-separated seeds instead of 1..runs")
    c.add_argument("--provider", choices=["mock", "replay", "deepseek"])
    c.add_argument("--realtime", action="store_true", help="wall-clock time (required for a real provider)")
    c.add_argument("--speed", type=float, default=1.0, help="simulator speed multiplier with --realtime")
    c.add_argument("--max-total-usd", type=float, default=0.25, help="real-spend cap across the whole comparison")
    c.add_argument("--out", default="runs")
    s = sub.add_parser("sim-serve", help="serve the simulated FRM API on a local port")
    s.add_argument("--scenario", default="scenarios/plates_and_rods.toml")
    s.add_argument("--port", type=int, default=18080)
    s.add_argument("--token", default="sim-token")
    s.add_argument("--speed", type=float, default=1.0)
    s.add_argument("--seed", type=int, default=0)
    cp = sub.add_parser("capture", help="read-only: save raw FRM responses and list machine IDs (P1)")
    cp.add_argument("--config", default="experiments/plates_and_rods.toml")
    cp.add_argument("--url", help="FRM base URL (loopback only); default frm.base_url")
    cp.add_argument("--endpoints", help="comma-separated read endpoints; default frm.read_endpoints")
    cp.add_argument("--timeout", type=float, default=5.0)
    cp.add_argument("--gap", type=float, default=0.5, help="seconds between requests (game-thread friendly)")
    cp.add_argument("--out", default="fixtures/live")
    rp = sub.add_parser("report", help="print a run report")
    rp.add_argument("run_dir")
    sub.add_parser("deepseek-models", help="list DeepSeek models (free endpoint, no tokens billed)")
    args = ap.parse_args(argv)
    if args.cmd == "report":
        print((Path(args.run_dir) / "report.md").read_text(encoding="utf-8"))
        return 0
    fn = {"run": _run, "compare": _compare, "sim-serve": _sim_serve, "capture": _capture, "deepseek-models": _deepseek_models}[args.cmd]
    try:
        return asyncio.run(fn(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
