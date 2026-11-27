"""Run report: production vs targets, action outcomes, conflicts, overrides, usage."""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Any

from ..context import item_name

if TYPE_CHECKING:
    from ..runner import Runner


def build_report(r: "Runner") -> dict[str, Any]:
    ring = list(r.log.ring)
    by_type = Counter(e["type"] for e in ring)
    val_codes = Counter(e["code"] for e in ring if e["type"] == "validation")
    arb_denials = Counter(e["code"] for e in ring if e["type"] == "arbitration" and e.get("result") == "denied")
    proposals_by_source = Counter(e["source"] for e in ring if e["type"] == "proposal" and e.get("source") != "harness")
    verif = Counter(e["result"] for e in ring if e["type"] == "verification" and e.get("source") != "harness")
    cmd_status = Counter(e["status"] for e in ring if e["type"] == "command_response" and e.get("source") != "harness")
    latencies = [e["latency_s"] for e in ring if e["type"] == "verification" and e.get("result") == "verified"]
    t0 = r.main_started or 0.0
    production = {}
    for it, n in r.targets_all.items():
        reached = r.outcome.reached_at.get(it)
        production[it] = {"item": item_name(it), "target": n, "delivered": r.outcome.total(it), "met": r.outcome.total(it) >= n, "reached_after_s": round(reached - t0, 1) if reached is not None else None}
    elapsed = (r.clock.now() - t0) if r.main_started is not None else 0.0
    all_met = bool(production) and all(p["met"] for p in production.values())
    within = all_met and all((p["reached_after_s"] or 0) <= r.cfg.experiment.duration_s for p in production.values())
    ctrl = r.controller.summary()
    # power: per-switch on-time (from observed state), shedding, fuses, power denials
    end_t = r.clock.now()
    switch_on_s: dict[str, float] = {}
    for alias, b in r.cfg.targets.items():
        if b.kind != "switch":
            continue
        tl = [e for e in r.machine_timeline if e["alias"] == alias]
        total = 0.0
        for i, e in enumerate(tl):
            if e["state"] != "on":
                continue
            start = max(e["t"], t0)
            stop = tl[i + 1]["t"] if i + 1 < len(tl) else end_t
            total += max(0.0, stop - start)
        switch_on_s[alias] = round(total, 1)
    power_report = {
        "switch_on_s_main_phase": switch_on_s,
        "sheds": [e for e in ring if e["type"] == "power_shed"],
        "fuse_trips": [e for e in ring if e["type"] == "power_event" and e.get("event") == "fuse_tripped"],
        "power_denials": {k: v for k, v in arb_denials.items() if k.startswith("POWER")},
        "advisories": [e for e in ring if e["type"] == "advisory"],
    }
    summary = {
        "run_id": r.run_id,
        "experiment": r.cfg.experiment.name,
        "controller": r.controller.name,
        "status": r.status,
        "elapsed_s": round(elapsed, 1),
        "targets_met": all_met,
        "targets_met_within_duration": within,
        "harness_constraint_violations": len(r.harness_violations),
        "max_running": r.max_running,
        "proposals": sum(proposals_by_source.values()),
        "commands_sent": sum(cmd_status.values()),
        "verified": verif.get("verified", 0),
        "unverified": verif.get("unverified", 0),
        "conflicts": by_type.get("conflict", 0),
        "human_overrides": by_type.get("human_override", 0),
        "alerts": len(r.alerts),
        "llm_calls": by_type.get("llm_response", 0),
        "llm_errors": by_type.get("llm_error", 0),
        "invalid_outputs": by_type.get("invalid_output", 0),
        "real_cost_usd": r.budget.real.spent_usd,
        "simulated_cost_usd": round(r.budget.simulated.spent_usd, 8),
    }
    return {
        "summary": summary,
        "production": production,
        "removed_externally": r.outcome.removed,
        "validation_codes": dict(val_codes),
        "arbitration_denials": dict(arb_denials),
        "proposals_by_source": dict(proposals_by_source),
        "command_status": dict(cmd_status),
        "verification": dict(verif),
        "verify_latency_s": {"n": len(latencies), "max": max(latencies) if latencies else None, "mean": round(sum(latencies) / len(latencies), 3) if latencies else None},
        "conflicts": [e for e in ring if e["type"] == "conflict"][-50:],
        "human_overrides": [e for e in ring if e["type"] == "human_override"],
        "constraint_violations_by_human": [e for e in ring if e["type"] == "constraint_violation"],
        "harness_violations": r.harness_violations,
        "stop": [e for e in ring if e["type"] == "stop"],
        "restore": r.restore_result,
        "alerts": r.alerts[-50:],
        "budget": r.budget.summary(),
        "power": power_report,
        "controller": ctrl,
        "frm_client": vars(r.client.stats),
        "event_counts": dict(by_type),
        "machine_timeline": r.machine_timeline,
    }


def report_markdown(rep: dict[str, Any]) -> str:
    s = rep["summary"]
    lines = [
        f"# Run {s['run_id']} - {s['experiment']} ({s['controller']})",
        "",
        f"Status: **{s['status']}** | elapsed {s['elapsed_s']} s | targets met: **{s['targets_met']}** (within duration: {s['targets_met_within_duration']})",
        "",
        "## Production (measured from storage deltas)",
        "",
        "| Item | Target | Delivered | Reached after (s) |",
        "|---|---|---|---|",
    ]
    for p in rep["production"].values():
        lines.append(f"| {p['item']} | {p['target']} | {p['delivered']} | {p['reached_after_s'] if p['reached_after_s'] is not None else '-'} |")
    lines += [
        "",
        "## Actions",
        "",
        f"- proposals: {s['proposals']} by source {rep['proposals_by_source']}",
        f"- validation codes: {rep['validation_codes']}",
        f"- arbitration denials: {rep['arbitration_denials']}",
        f"- commands sent: {s['commands_sent']} -> verified {s['verified']}, unverified {s['unverified']} (latency {rep['verify_latency_s']})",
        f"- conflicts: {s['conflicts']} | human overrides: {s['human_overrides']} | harness constraint violations: {s['harness_constraint_violations']} | max running per group: {s['max_running']}",
        "",
        "## Power",
        "",
        f"- switch on-time in main phase (s): {rep['power']['switch_on_s_main_phase'] or 'no switches controlled'}",
        f"- load sheds: {len(rep['power']['sheds'])} | fuse trips: {len(rep['power']['fuse_trips'])} | power denials: {rep['power']['power_denials']}",
        f"- advisories to the human: {[a['agent'] + ': ' + a['message'] for a in rep['power']['advisories']] or 'none'}",
        "",
        "## Model usage",
        "",
        f"- calls: {s['llm_calls']} | errors: {s['llm_errors']} | invalid outputs: {s['invalid_outputs']}",
        f"- real spend: ${s['real_cost_usd']:.6f} | simulated (mock) cost estimate: ${s['simulated_cost_usd']:.6f}",
        f"- budget: {rep['budget']}",
        "",
        "## Stop / restore",
        "",
        f"- stop: {[{k: e.get(k) for k in ('reason', 'source', 'mode')} for e in rep['stop']] or 'none'}",
        f"- restore: {rep['restore']}",
        "",
        "## Alerts",
        "",
    ]
    lines += [f"- t={a['t']} [{a['level']}] {a['message']}" for a in rep["alerts"]] or ["- none"]
    return "\n".join(lines) + "\n"
