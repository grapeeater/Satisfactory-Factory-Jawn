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

## Using real AI agents (DeepSeek)

Out of the box the agents are mock models: free, offline and deterministic. To run real AI agents
you need a DeepSeek API key. They are cheap: the P3 test made 156 model calls for about $0.03 in total.

### 1. Get an API key

1. Sign up at <https://platform.deepseek.com>.
2. Open **Top up** and add a small amount of credit.
   - DeepSeek is prepaid, so you can't be charged more than your balance.
   - With no balance, calls fail with "insufficient balance".
3. Open **API keys** → **Create new API key**, and name it something like `factory-jawn`.
4. Copy the key, which starts with `sk-`. **It is shown only once.** If you lose it, delete it and create a new one.

Treat the key like a password. Never paste it into a config file, chat or anything in this repo.

### 2. Put the key in

Store it as a Windows user environment variable. In PowerShell, replace `sk-your-key-here` with your key:

```powershell
[Environment]::SetEnvironmentVariable('DEEPSEEK_API_KEY', 'sk-your-key-here', 'User')
```

**Close and reopen your terminal** so it picks up the variable. Then check that it works. This call is free and uses no tokens:

```powershell
uv run jawn deepseek-models      # should print the model names
```

If it says `DEEPSEEK_API_KEY not set`, you're still in the old terminal.

### 3. Enable paid calls

Paid calls are off by default. A run only spends money when **both** switches are on:

1. **The experiment config allows it.** In `[llm.deepseek]` set `allow_paid = true`, and give `[budget]` a non-zero `max_usd`, which is the hard cap for the run.
   - `experiments/sim_deepseek_plates_and_rods.toml` already has this, with a $0.06 cap per run.
   - The other shipped configs have it off.
2. **You turn it on in that terminal:**

   ```powershell
   $env:JAWN_ALLOW_PAID_CALLS = 1
   ```

### 4. Run

Real models must run on wall-clock time (`--realtime`); the harness refuses otherwise.

```powershell
uv run jawn compare --config experiments/sim_deepseek_plates_and_rods.toml --provider deepseek --realtime --speed 4 --runs 3 --max-total-usd 0.25
```

- Every call reserves its worst-case cost first, so a run stops before it passes its cap.
- `--max-total-usd` caps the spend across all the runs.
- The actual cost is in each run's report in `runs/<run-id>/`.

To switch paid calls off again, close the terminal or run `Remove-Item Env:JAWN_ALLOW_PAID_CALLS`.

**To change the key:** repeat step 2 with the new key, then delete the old one on DeepSeek's API keys page.

**To remove the key from the PC:**

```powershell
[Environment]::SetEnvironmentVariable('DEEPSEEK_API_KEY', $null, 'User')
```

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
