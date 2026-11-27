"""Run orchestration: observe -> decide -> validate -> arbitrate -> execute -> verify -> log.

Phases: connect (wait for trustworthy observations) -> takeover (record Dave's state) ->
setup (apply experiment initial state, verified) -> main loop -> stop/restore -> report.

Emergency stop sources: dashboard button, STOP file, in-game chat ("!jawn stop"), Ctrl-C
(task cancellation), budget hard stop. Stop never pauses the factory: it cancels model
calls, sends no new agent actions, and (mode "restore") returns machines the harness
changed to the state they had when the harness took over, verifying each one.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .clock import Clock
from .config import Config
from .context import TickContext, item_name
from .control.actions import FRM_CAPABILITIES, ActionKind, Proposal, SPECS
from .control import power
from .control.arbiter import Arbiter
from .control.executor import Executor, Verifier
from .control.human import ChatCommands, OverrideDetector, StopController
from .control.state import ControlState
from .control.validator import Validator
from .events.log import EventLog
from .frm.client import FrmClient, FrmError
from .llm.budget import BudgetMeter
from .observe.observer import Observer
from .observe.outcome import OutcomeMeter
from .observe.snapshot import WRITE_SAFE, Health, Snapshot


class Controller(Protocol):
    name: str

    async def step(self, ctx: TickContext) -> list[Proposal]: ...

    def on_result(self, proposal: Proposal, stage: str, code: str, detail: str) -> None: ...

    def summary(self) -> dict[str, Any]: ...

    async def shutdown(self) -> None: ...


@dataclass
class RunResult:
    run_id: str
    status: str
    report: dict[str, Any] = field(default_factory=dict)


class Runner:
    def __init__(self, cfg: Config, clock: Clock, client: FrmClient, controller: Controller, budget: BudgetMeter, run_dir: Path | None = None, run_id: str | None = None, observe_only: bool = False) -> None:
        self.observe_only = observe_only  # P1 smoke tests: observe and log, never write
        self.cfg = cfg
        self.clock = clock
        self.client = client
        self.controller = controller
        self.budget = budget
        self.run_id = run_id or time.strftime("%Y%m%d-%H%M%S")
        self.run_dir = run_dir
        self.log = EventLog(clock, (run_dir / "events.jsonl") if run_dir else None, ring=50000, run_id=self.run_id)
        self.st = ControlState(dict(cfg.targets))
        self.observer = Observer(client, clock, cfg.frm.read_endpoints, cfg.safety)
        if controller.name == "baseline":
            controlled = sorted({t for a in cfg.agents for t in a.owns})
            authority = {"baseline": set(controlled)}
            allowed = {"baseline": {ActionKind.MACHINE_ENABLE, ActionKind.MACHINE_DISABLE, ActionKind.SWITCH_ON, ActionKind.SWITCH_OFF}}
        else:
            authority = {a.id: set(a.owns) for a in cfg.agents}
            allowed = {a.id: {ActionKind(k) for k in a.allowed_actions} for a in cfg.agents}
        self.validator = Validator(cfg.safety, authority, allowed, FRM_CAPABILITIES)
        self.arbiter = Arbiter(cfg.constraints, cfg.safety)
        self.executor = Executor(client, clock, self.st, cfg.safety, self.log.emit, lambda: self.observer.seq)
        self.verifier = Verifier(self.st, cfg.safety, self.log.emit)
        self.detector = OverrideDetector(cfg.safety)
        self.chat = ChatCommands(cfg.safety.chat_prefix)
        stop_file = cfg.resolve(cfg.safety.stop_file) if cfg.safety.stop_file else None
        self.stop = StopController(cfg.safety.on_stop, stop_file)
        items = sorted({it for a in cfg.agents for it in a.targets} | {b.product for b in cfg.targets.values() if b.product})
        self.targets_all = {it: n for a in cfg.agents for it, n in a.targets.items()}
        self.outcome = OutcomeMeter(items)
        self.status = "init"
        self.main_started: float | None = None
        self.snap: Snapshot | None = None
        self.health: Health | None = None
        self.alerts: list[dict[str, Any]] = []
        self.actions: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.max_running: dict[str, int] = {c.name: 0 for c in cfg.constraints}
        self.harness_violations: list[dict[str, Any]] = []
        self.restore_result: dict[str, Any] = {}
        self.report: dict[str, Any] = {}
        self._targets_met_at: float | None = None
        self.machine_timeline: list[dict[str, Any]] = []
        self._last_cond: dict[str, str] = {}
        self._fuse: dict[int, bool] = {}

    # ------------------------------------------------------------------ helpers
    def alert(self, level: str, message: str, **data: Any) -> None:
        a = {"t": round(self.clock.now(), 3), "level": level, "message": message, **data}
        self.alerts.append(a)
        self.alerts = self.alerts[-200:]
        self.log.emit("alert", level=level, message=message, **data)

    def _track_action(self, p: Proposal, **upd: Any) -> None:
        rec = self.actions.get(p.id)
        if rec is None:
            rec = {"id": p.id, "decision_id": p.decision_id, "t": round(p.created_at, 3), "source": p.source, "kind": p.kind.value, "target": p.target, "reason": p.reason, "stages": []}
            self.actions[p.id] = rec
            while len(self.actions) > 300:
                self.actions.popitem(last=False)
        rec.update({k: v for k, v in upd.items() if k != "stage"})
        if "stage" in upd:
            rec["stages"].append(upd["stage"])
        rec["status"] = upd.get("status", rec.get("status"))

    def _ctx(self, snap: Snapshot) -> TickContext:
        now = self.clock.now()
        elapsed = now - (self.main_started if self.main_started is not None else now)
        return TickContext(now, snap, self.st, self.cfg, self.outcome, elapsed, self.cfg.experiment.duration_s - elapsed)

    def _update_product_map(self) -> None:
        self.outcome.product_of = {self.st.resolved[a]: b.product for a, b in self.cfg.targets.items() if b.product and a in self.st.resolved}

    # ------------------------------------------------------------------ observation processing
    async def observe(self) -> Snapshot:
        snap = await self.observer.poll()
        now = self.clock.now()
        self.snap = snap
        if snap.health != self.health:
            self.log.emit("health", health=snap.health.value, previous=self.health.value if self.health else None, errors=snap.errors, level="info" if snap.health in WRITE_SAFE else "warn")
            if snap.health == Health.NOT_FOUND:
                self.alert("error", "FRM answers 404 on every endpoint (known issue after session reload): restart the game; agents are frozen")
            elif snap.health == Health.GAME_PAUSED:
                self.alert("warn", "game time is not advancing (pause menu or frozen data); no actions will be taken")
            elif snap.health in (Health.DOWN, Health.STALE):
                self.alert("error", f"observations unavailable ({snap.health.value}); all writes suspended")
            self.health = snap.health
        if snap.errors:
            self.log.emit("snapshot_errors", seq=snap.seq, errors=snap.errors, level="warn")
        self.log.emit("snapshot", **snap.summary())
        for ev in self.st.resolve(snap):
            t = ev.pop("type")
            self.log.emit(t, **ev, level="warn")
            if t == "target_rebound":
                self.alert("warn", f"{ev['alias']} re-bound to {ev['new_id']} (ID changed, matched by class/recipe/location)")
            elif t == "target_missing":
                self.alert("error", f"{ev['alias']} not found in observations; no actions on it")
        self._update_product_map()
        power.learn_topology(snap, self.st)
        for cmd in self.verifier.update(snap, now):
            stage = "verification" if cmd.status == "verified" else "verification_failed"
            self._track_action(cmd.proposal, status=cmd.status, stage=f"{stage}@{now:.0f}")
            if cmd.status != "verified":
                self.alert("error", f"{cmd.alias}: command {cmd.id} not reflected in game state within {self.cfg.safety.verify_timeout_s:.0f}s", command=cmd.id)
            self.controller.on_result(cmd.proposal, stage, cmd.status.upper(), f"observed {cmd.attr}={self.st.observe(snap, cmd.alias)}")
        for ov in self.detector.update(snap, self.st, now):
            lock_s = ov["locked_until"] - now
            if ov.get("cause") == "priority_shed":
                self.log.emit("power_shed", **ov, level="warn")
                self.alert("warn", f"priority switch {ov['alias']} opened by itself (game load shedding?); locked for {lock_s:.0f}s")
                continue
            self.log.emit("human_override", **ov, level="warn")
            if self.cfg.targets[ov["alias"]].kind == "switch":
                desc = "on" if ov["to"] else "off"
            else:
                desc = "paused" if ov["to"] else "running"
            self.alert("warn", f"human changed {ov['alias']} ({desc}); locked for {lock_s:.0f}s")
        for alias, b in self.cfg.targets.items():
            if b.kind == "switch" and alias not in self.st.initial_priority:
                v = self.st.observe_attr(snap, alias, "priority")
                if v is not None:
                    self.st.initial_priority[alias] = v
        if self.cfg.safety.chat_commands:
            for c in self.chat.update(snap):
                self.log.emit("chat_command", verb=c.verb, args=c.args, sender=c.sender)
                await self._chat_command(c.verb, c.sender)
        self.stop.check_file()
        for an in self.outcome.update(snap, self.targets_all):
            self.log.emit("outcome_anomaly", **an, level="warn")
            self.alert("warn", f"{an['machine']} reports producing but no {item_name(an['item'])} reached storage in {an['window_s']:.0f}s")
        for c in self.cfg.constraints:
            if self.status != "running":
                continue  # constraints apply to the experiment's main phase only
            running = [a for a in c.targets if self.st.observe(snap, a) is False]
            self.max_running[c.name] = max(self.max_running[c.name], len(running))
            if len(running) > c.max and all(self.st.harness_set.get(a, False) for a in running):
                self.harness_violations.append({"t": round(now, 3), "constraint": c.name, "running": running})
        for cid, circ in snap.circuits.items():
            was = self._fuse.get(cid)
            if circ.fuse and not was:
                self.log.emit("power_event", circuit=cid, event="fuse_tripped", capacity=circ.capacity, level="error")
                self.alert("error", f"power fuse tripped on circuit {cid}: FRM cannot reset fuses, a human must reset it in game")
            elif was and not circ.fuse:
                self.log.emit("power_event", circuit=cid, event="fuse_reset")
                self.alert("info", f"power restored on circuit {cid}")
            self._fuse[cid] = circ.fuse
        for alias, b in self.cfg.targets.items():
            if b.kind == "switch":
                sw = snap.switches.get(self.st.resolved.get(alias, ""))
                cond = "missing" if sw is None or sw.on is None else ("on" if sw.on else "off")
            else:
                m = snap.machines.get(self.st.resolved.get(alias, ""))
                cond = m.condition() if m else "missing"
            if self._last_cond.get(alias) != cond:
                self._last_cond[alias] = cond
                self.machine_timeline.append({"t": round(now, 3), "alias": alias, "state": cond})
        return snap

    async def _chat_command(self, verb: str, sender: str) -> None:
        if verb == "stop":
            self.stop.trigger(f"chat command from {sender}", "chat")
        elif verb == "release":
            self.stop.trigger(f"chat release from {sender}", "chat", "restore")
        elif verb in ("hold", "pause"):
            self.st.held = True
            self.alert("warn", f"writes on hold (chat, {sender})")
        elif verb == "resume":
            self.st.held = False
            self.alert("info", f"writes resumed (chat, {sender})")
        await self._announce(f"Jawn: {verb} acknowledged")

    async def _announce(self, text: str) -> None:
        if not self.cfg.safety.announce_in_chat or "sendChatMessage" not in self.cfg.frm.write_endpoints:
            return
        try:
            await self.client.post("sendChatMessage", {"message": text[:120], "sender": "Jawn"})
        except FrmError as e:
            self.log.emit("announce_failed", error=str(e), level="warn")

    # ------------------------------------------------------------------ control
    async def control(self, snap: Snapshot, proposals: list[Proposal]) -> None:
        now = self.clock.now()
        valid: list[Proposal] = []
        for p in proposals:
            self.log.emit("proposal", **p.to_dict())
            self._track_action(p, status="proposed")
            v = self.validator.validate(p, snap, self.st, now)
            self.log.emit("validation", proposal_id=p.id, decision_id=p.decision_id, source=p.source, **v.to_dict(), level="info" if v.ok or v.code in ("NOOP", "WAIT") else "warn")
            if v.ok:
                valid.append(p)
                self._track_action(p, status="valid", stage="valid")
            else:
                self._track_action(p, status=f"rejected:{v.code}", stage=f"rejected:{v.code}", detail=v.detail)
                self.controller.on_result(p, "validation", v.code, v.detail)
        out = self.arbiter.arbitrate(valid, snap, self.st, now)
        for viol in out.violations:
            self.log.emit("constraint_violation", **viol, level="warn")
            self.alert("warn", f"constraint {viol['constraint']} exceeded by human action ({', '.join(viol['caused_by_human']) or 'unknown'}); not correcting")
        for c in out.conflicts:
            self.log.emit("conflict", **c)
        for d in out.denials:
            self.log.emit("arbitration", proposal_id=d.proposal.id, decision_id=d.proposal.decision_id, source=d.proposal.source, result="denied", code=d.code, detail=d.detail)
            self._track_action(d.proposal, status=f"denied:{d.code}", stage=f"denied:{d.code}", detail=d.detail)
            self.controller.on_result(d.proposal, "arbitration", d.code, d.detail)
        for p in out.grants:
            self.log.emit("arbitration", proposal_id=p.id, decision_id=p.decision_id, source=p.source, result="granted")
            self._track_action(p, status="granted", stage="granted")
            cmd = await self.executor.execute(p)
            self._track_action(p, status=cmd.status, stage=f"sent:{cmd.status}", command=cmd.id)
            if cmd.status in ("error", "cancelled"):
                self.alert("error", f"{p.target}: write failed ({cmd.error or cmd.status})")
                self.controller.on_result(p, "command", cmd.status.upper(), cmd.error or "")

    async def _apply_system(self, alias: str, want_paused: bool | None, want_on: bool | None, reason: str, restore: bool) -> str:
        snap = self.snap
        assert snap is not None
        b = self.cfg.targets[alias]
        if b.kind == "machine":
            kind = ActionKind.MACHINE_DISABLE if want_paused else ActionKind.MACHINE_ENABLE
        else:
            kind = ActionKind.SWITCH_ON if want_on else ActionKind.SWITCH_OFF
        p = Proposal("harness", kind, alias, reason, snap.seq, snap.taken_at, self.clock.now(), system=True, params={"stop_restore": restore})
        self.log.emit("proposal", **p.to_dict())
        v = self.validator.validate(p, snap, self.st, self.clock.now())
        self.log.emit("validation", proposal_id=p.id, source=p.source, **v.to_dict())
        if not v.ok:
            return v.code
        cmd = await self.executor.execute(p)
        self._track_action(p, status=cmd.status, stage=f"sent:{cmd.status}")
        return cmd.status

    async def _converge(self, desired: dict[str, bool], reason: str, restore: bool, timeout: float) -> dict[str, str]:
        """Drive targets to desired observed values (machines: paused, switches: on), verifying
        each; bounded by timeout."""
        deadline = self.clock.now() + timeout
        status: dict[str, str] = {a: "pending" for a in desired}
        while self.clock.now() <= deadline:
            snap = await self.observe()
            for alias, want in desired.items():
                obs = self.st.observe(snap, alias)
                if obs == want and alias not in self.st.pending:
                    status[alias] = "ok"
                    continue
                if self.st.locked(alias, self.clock.now()):
                    status[alias] = "skipped:human_lock"
                    continue
                if alias in self.st.pending or snap.health not in WRITE_SAFE:
                    continue
                margin = self.cfg.safety.power_margin
                is_switch = self.cfg.targets[alias].kind == "switch"
                # never let a system move (setup / restore) overload a grid
                if is_switch:
                    risk = power.check_switch_off(snap, self.st, alias, margin) if want is False else power.check_switch_on(snap, self.st, alias, margin, {})
                else:
                    risk = power.check_machine_enable(snap, self.st, alias, margin) if want is False else None
                if risk is not None and risk[0] in ("POWER_HEADROOM", "POWER_FUSE"):
                    status[alias] = f"skipped:{risk[0]}"
                    continue
                if is_switch:
                    r = await self._apply_system(alias, None, want, reason, restore)
                else:
                    r = await self._apply_system(alias, want, None, reason, restore)
                status[alias] = f"sent:{r}" if r in ("sent", "uncertain") else r
            if all(s == "ok" or s.startswith("skipped") for s in status.values()):
                break
            await self.clock.sleep(min(self.cfg.experiment.poll_interval_s, 2.0))
        return status

    # ------------------------------------------------------------------ lifecycle
    async def run(self) -> RunResult:
        self.log.emit("run_start", run_id=self.run_id, experiment=self.cfg.experiment.name, controller=self.controller.name, provider=getattr(getattr(self.controller, "provider", None), "name", None), frm=self.cfg.frm.base_url, duration_s=self.cfg.experiment.duration_s)
        try:
            ok = await self._connect()
            if ok and not self.stop.stopped and self.observe_only:
                await self._observe_only()
            elif ok and not self.stop.stopped:
                ok = await self._setup()
                if ok and not self.stop.stopped:
                    await self._main()
        except asyncio.CancelledError:
            self.stop.trigger("interrupted (Ctrl-C / cancellation)", "signal")
            task = asyncio.current_task()
            if task is not None:
                task.uncancel()
        except Exception as e:  # noqa: BLE001 - always try to leave the factory in a good state
            self.alert("error", f"harness crashed: {type(e).__name__}: {e}")
            self.stop.trigger(f"internal error: {type(e).__name__}", "crash")
        finally:
            await self._shutdown()
        return RunResult(self.run_id, self.status, self.report)

    async def _connect(self) -> bool:
        self.status = "connecting"
        deadline = self.clock.now() + max(30.0, self.cfg.experiment.poll_interval_s * 4)
        while True:
            snap = await self.observe()
            if snap.health in WRITE_SAFE and not (set(self.cfg.targets) & self.st.missing):
                self.log.emit("takeover", initial={a: v for a, v in self.st.initial.items()}, resolved=dict(self.st.resolved))
                return True
            if self.clock.now() >= deadline or self.stop.stopped:
                self.alert("error", f"could not get trustworthy observations (health={snap.health.value}, missing={sorted(self.st.missing)}); aborting without any writes")
                self.status = "aborted"
                return False
            await self.clock.sleep(self.cfg.experiment.poll_interval_s)

    async def _setup(self) -> bool:
        self.status = "setup"
        desired = self.cfg.desired_initial()
        if desired:
            res = await self._converge(desired, "experiment initial state", restore=False, timeout=max(30.0, self.cfg.safety.verify_timeout_s * 2))
            self.log.emit("setup", result=res)
            if not all(v == "ok" for v in res.values()):
                self.alert("error", f"setup did not converge: {res}")
                self.status = "aborted"
                return False
        snap = await self.observe()
        self.outcome = OutcomeMeter(self.outcome.tracked_items)  # measure from the start of the main phase
        self._update_product_map()
        self.outcome.update(snap, self.targets_all)
        return True

    async def _main(self) -> None:
        self.status = "running"
        self.main_started = self.clock.now()
        self.st.last_change.clear()  # setup moves are not agent decisions; no anti-flap dwell from them
        self.log.emit("main_start", t0=self.main_started)
        while not self.stop.stopped:
            snap = await self.observe()
            if self.stop.stopped:
                break
            ctx = self._ctx(snap)
            proposals = await self.controller.step(ctx)
            for adv in getattr(self.controller, "drain_advisories", lambda: [])():
                self.alert("info", f"advisory from {adv['agent']}: {adv['message']}", agent=adv["agent"])
                await self._announce(f"[{adv['agent']}] {adv['message']}")
            await self.control(snap, proposals)
            if ctx.elapsed >= self.cfg.experiment.duration_s:
                self.log.emit("end_condition", reason="duration")
                break
            if self.cfg.experiment.end_when_targets_met and self.targets_all and all(self.outcome.total(i) >= n for i, n in self.targets_all.items()):
                settled = not self.st.pending and all(self.st.observe(snap, a) is True for a in self._controlled())
                if settled:
                    self._targets_met_at = self._targets_met_at or self.clock.now()
                    if self.clock.now() - self._targets_met_at >= self.cfg.experiment.settle_s:
                        self.log.emit("end_condition", reason="targets_met")
                        break
            await self.clock.sleep(self.cfg.experiment.poll_interval_s)

    async def _observe_only(self) -> None:
        """No setup, no controller, no writes: observation, health and outcome logging only."""
        self.status = "observing"
        self.st.held = True
        self.main_started = self.clock.now()
        self.log.emit("main_start", t0=self.main_started, observe_only=True)
        while not self.stop.stopped and self.clock.now() - self.main_started < self.cfg.experiment.duration_s:
            await self.observe()
            await self.clock.sleep(self.cfg.experiment.poll_interval_s)

    def _controlled(self) -> list[str]:
        """Controlled machines (end-of-run 'parked' check); switches are infrastructure."""
        return sorted({t for a in self.cfg.agents for t in a.owns if self.cfg.targets[t].kind == "machine"})

    async def _shutdown(self) -> None:
        prev = self.status
        if self.stop.stopped:
            req = self.stop.request
            assert req is not None
            self.status = "stopping"
            self.st.stopped = True
            self.log.emit("stop", reason=req.reason, source=req.source, mode=req.mode, level="warn")
            self.alert("warn", f"EMERGENCY STOP ({req.source}): {req.reason}; mode={req.mode}")
        else:
            self.status = "finishing"
            self.st.stopped = True
        try:
            await self.controller.shutdown()
        except Exception as e:  # noqa: BLE001
            self.log.emit("controller_shutdown_error", error=str(e), level="error")
        mode = self.stop.request.mode if self.stop.request else self.cfg.safety.on_stop
        if not self.st.history:
            self.restore_result = {"mode": mode, "skipped": "no writes were made"}
        elif mode == "restore":
            await self._restore()
        else:
            # hold: no further writes; let in-flight commands resolve so the log is complete
            self.restore_result = {"mode": "hold", "result": "left as-is"}
            if self.st.pending:
                with _suppress():
                    for _ in range(int(self.cfg.safety.verify_timeout_s // 2) + 1):
                        if not self.st.pending:
                            break
                        await self.observe()
                        await self.clock.sleep(2.0)
        self.log.emit("restore", **self.restore_result)
        try:
            await self.observe()
        except Exception:  # noqa: BLE001
            pass
        self.status = "stopped" if self.stop.stopped else ("aborted" if prev == "aborted" else "done")
        from .eval.report import build_report

        self.report = build_report(self)
        self.log.emit("run_end", status=self.status, summary=self.report.get("summary"))
        if self.run_dir is not None:
            (self.run_dir / "report.json").write_text(json.dumps(self.report, indent=2, default=str), encoding="utf-8")
            from .eval.report import report_markdown

            (self.run_dir / "report.md").write_text(report_markdown(self.report), encoding="utf-8")
        self.log.close()
        await self.client.close()

    async def _restore(self) -> None:
        machines: dict[str, bool] = {}
        switches: dict[str, bool] = {}
        skipped: dict[str, str] = {}
        for alias, init in self.st.initial.items():
            b = self.cfg.targets.get(alias)
            if b is None or init is None:
                continue
            if not self.st.harness_set.get(alias, False):
                skipped[alias] = "not changed by harness (or changed by human since)"
                continue
            (switches if b.kind == "switch" else machines)[alias] = bool(init)
        # power-safe order: shed load first (machine disables), then add supply (switch
        # closes), then add load (machine enables, each power-checked), then open switches
        # (each checked against the learned topology). In-flight commands are resolved by
        # the verifier during convergence.
        timeout = self.cfg.safety.stop_restore_timeout_s
        reason = "stop: restore pre-harness state"
        stages = [
            {a: v for a, v in machines.items() if v is True},
            {a: v for a, v in switches.items() if v is True},
            {a: v for a, v in machines.items() if v is False},
            {a: v for a, v in switches.items() if v is False},
        ]
        res: dict[str, str] = {}
        if self.st.pending and not any(stages):
            res.update(await self._converge({}, reason, restore=True, timeout=timeout))
        for stage in stages:
            if stage:
                res.update(await self._converge(stage, reason, restore=True, timeout=timeout))
        # switch priorities the harness changed go back to their pre-harness value
        if self.st.priority_set and self.snap is not None:
            snap = await self.observe()
            for alias in sorted(self.st.priority_set):
                init = self.st.initial_priority.get(alias)
                if not init or self.st.observe_attr(snap, alias, "priority") == init or alias in self.st.pending:
                    res[f"{alias}:priority"] = "ok"
                    continue
                p = Proposal("harness", ActionKind.SWITCH_SET_PRIORITY, alias, reason, snap.seq, snap.taken_at, self.clock.now(), system=True, params={"stop_restore": True, "value": int(init)})
                self.log.emit("proposal", **p.to_dict())
                v = self.validator.validate(p, snap, self.st, self.clock.now())
                self.log.emit("validation", proposal_id=p.id, source=p.source, **v.to_dict())
                if not v.ok:
                    res[f"{alias}:priority"] = v.code
                    continue
                cmd = await self.executor.execute(p)
                res[f"{alias}:priority"] = f"sent:{cmd.status}"
        # let the last attempts resolve (verified or unverified) without sending anything new,
        # so no command is left dangling when the run ends
        deadline = self.clock.now() + self.cfg.safety.verify_timeout_s + 5.0
        while self.st.pending and self.clock.now() <= deadline:
            await self.clock.sleep(2.0)
            snap = await self.observe()
            for alias, want in {**machines, **switches}.items():
                if self.st.observe(snap, alias) == want and alias not in self.st.pending:
                    res[alias] = "ok"
        for alias in self.st.priority_set:
            key = f"{alias}:priority"
            if key in res and res[key].startswith("sent") and self.snap is not None:
                res[key] = "ok" if self.st.observe_attr(self.snap, alias, "priority") == self.st.initial_priority.get(alias) else "unverified"
        failed = {a: s for a, s in res.items() if s != "ok" and not s.startswith("skipped")}
        power_skips = {a: s for a, s in res.items() if s.startswith("skipped:POWER")}
        self.restore_result = {"mode": "restore", "restored": res, "skipped": skipped, "complete": not failed}
        if power_skips:
            self.alert("warn", f"not restored because it could overload a grid (please check in game): {sorted(power_skips)}")
        if failed:
            self.alert("error", f"restore incomplete, please check these machines in game: {failed}")


class _suppress:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: Any) -> bool:
        return exc[0] is not None and issubclass(exc[0], Exception)


__all__ = ["Runner", "RunResult", "Controller", "SPECS"]
