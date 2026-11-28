# Satisfactory-Factory-Jawn
Research and handoff for independent AI factory managers in Satisfactory

A harness in which several independent AI agents manage parts of a Satisfactory factory through
[Ficsit Remote Monitoring](https://ficsit.app/mod/FicsitRemoteMonitoring) (FRM), while the human
player keeps priority. Phases P0–P4 are built and tested offline against a simulated FRM server.
Live in-game testing (P1 onward) is next.

- Plan and research: [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md)
- P0 results, limitations and P1 readiness: [docs/P0_REPORT.md](docs/P0_REPORT.md)
- P1 live smoke-test runbook (owner steps): [docs/P1_RUNBOOK.md](docs/P1_RUNBOOK.md)
- P3 on the simulator, real DeepSeek agents vs baseline: [docs/P3_SIM_REPORT.md](docs/P3_SIM_REPORT.md)
- P4 power management, four agents, switch priorities, advisories: [docs/P4_REPORT.md](docs/P4_REPORT.md)
- Getting a DeepSeek API key, for real AI agents: [docs/DEEPSEEK_SETUP.md](docs/DEEPSEEK_SETUP.md)
- Original brief: [HANDOFF.txt](HANDOFF.txt)

## Quick start (offline, no game, no spend)

```powershell
uv sync                                    # Python 3.12+, installs aiohttp + pydantic (+ pytest)
uv run pytest -q                           # full test suite (~40 s)
uv run jawn run                            # first experiment, mock agents, simulator, deterministic
uv run jawn run --controller baseline      # rule-based baseline
uv run jawn compare --runs 5               # matched baseline-vs-agents runs over seeded variations
uv run jawn run --realtime --speed 10 --dashboard --linger 600   # watch it: http://127.0.0.1:8787/
uv run jawn run --config experiments/power_grid.toml            # P4: 4 agents on a power-limited factory
uv run jawn compare --config experiments/power_grid.toml --runs 6
```

Reports and JSONL event logs land in `runs/<run-id>/`.

## How it works

```
FRM (or simulator) --GET allowlist--> Observer --> Snapshot (health, carry-over, ages)
     ^                                                 |
     |                              +------------------+-------------------+
     |                              v                                      v
     |                  Agents (own objective, memory,           Baseline controller
     |                  history, inbox; event-driven             (deterministic rules)
     |                  model calls, budget-reserved)
     |                              |  proposals                           |
     |                              +------------------+-------------------+
     |                                                 v
     |                Validator (authority, capability, staleness, human lock, dwell, rate)
     |                                                 v
     |                Arbiter (human > power/fuse > constraints > deficit priority > fairness)
     |                                                 v
     +------ setEnabled ------------------- Executor (single writer, never retries writes)
                                                       v
                         Verifier (later observation) + Outcome meter (storage deltas)
                                                       v
                         Event log (JSONL) + report + dashboard
```

Key properties: decisions, validations, arbitration, game responses, readback and measured
outcomes are logged separately. Unknown state is treated as unsafe. Writes stop whenever
observations are unhealthy (stale, frozen, game paused, 404). Human changes lock the machine.
The emergency stop (dashboard, `STOP` file, in-game chat `!jawn stop`, Ctrl-C) cancels model
calls and restores machines the harness changed, verifying each one.

## Layout

| Path | What |
|---|---|
| `src/jawn/sim/` | factory simulator, simulated FRM HTTP server, fault injection |
| `src/jawn/frm/` | FRM client (allowlists, loopback-only, token handling) |
| `src/jawn/observe/` | observer, tolerant parsing, health, outcome meter |
| `src/jawn/control/` | actions/capabilities, validator, arbiter, power model (switch topology), executor/verifier, human overrides, stop |
| `src/jawn/agents/` | agent runtime, prompts, decision schema, event-driven scheduler |
| `src/jawn/llm/` | provider interface, mock/scripted/replay, DeepSeek adapter, pricing, budget |
| `src/jawn/baseline/` | deterministic baseline controller |
| `src/jawn/dashboard/` | local dashboard (loopback, token-protected controls) |
| `experiments/`, `scenarios/` | experiment configs, simulated factory layouts |

## Safety defaults

- `llm.deepseek.allow_paid = false` and `budget.max_usd = 0`. A paid call also needs
  `JAWN_ALLOW_PAID_CALLS=1`. With the shipped configs no request can reach DeepSeek.
- Live FRM writes need `--live` plus `JAWN_ALLOW_LIVE_WRITES=1`; `--observe-only` never writes.
- FRM, the simulator and the dashboard only bind to or connect to loopback by default.
