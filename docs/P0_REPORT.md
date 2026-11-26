# P0 report: offline harness

Status: **P0 complete**, 2026-11-26. Everything below was run on this PC against the simulator.
Nothing was installed into the game, the game was not launched, firewall settings were not
changed, and no paid API call was made.

The one network call to DeepSeek was `GET /models`, which is free. It was used to verify the
model identifier.

## 1. What was built

| Area | Implementation |
|---|---|
| Simulated FRM | `sim/world.py` factory model, `sim/frm_server.py` HTTP server, `sim/faults.py` runtime fault injection. See below. |
| Independent agents | Each `AgentRuntime` (`agents/agent.py`) has its own objective, owned machines, private memory note, its own decision history, an inbox for explicit messages, and its own wake triggers. No agent's prompt ever contains another agent's memory or reasoning; a test enforces this. |
| Event-driven scheduling | An agent is woken only when one of these happens: its salient state signature changes, a message arrives, it gets a denial or failure result, or the heartbeat fires (180 s). Each agent has a minimum interval (15 s), and the controller caps concurrency. Steady production triggers no calls. |
| Spending limits | `llm/budget.py` reserves the worst-case cost before every call and refuses calls over the cap. It enforces per-run and per-agent call caps, and a provider 402 response hard-stops all agents. Real and simulated usage are kept in separate ledgers. When an agent's budget runs out it falls back to the baseline controller or holds, as configured. |
| DeepSeek adapter | `llm/deepseek.py`: model `deepseek-flash` (verified), thinking disabled, strict tool calling via `/beta` or JSON mode, usage and cache accounting, peak/off-peak pricing, and retry rules: 429/5xx retried with backoff, 402/401 never retried. It is tested only against a local fake server. |
| Baseline | `baseline/policy.py`: deterministic rules. It sends its proposals through the same validator, arbiter and executor as the agents. |
| Validation | `control/validator.py`: 18 distinct rejection codes. Examples: NOT_OWNER, UNSUPPORTED_CAPABILITY, STALE_DECISION, STATE_CHANGED, HUMAN_LOCK, DWELL, PENDING_COMMAND, HEALTH, RATE_LIMIT, NOOP. |
| Arbitration | `control/arbiter.py`: deterministic. Disables are always granted. Enables are checked against a pessimistic projection that counts pending commands and treats unknown state as running. Power headroom and tripped fuses are respected. Conflicts are recorded. Constraint violations caused by a human are reported but not corrected. |
| Verification | `control/executor.py`: one writer at a time, and writes are never retried. A command counts only once a later observation verifies it. Timeouts are marked "uncertain" and resolved by readback, never re-sent. `observe/outcome.py` measures delivery from storage deltas and flags "producing without delivery". |
| Human priority | `control/human.py`: any state change the harness did not command is treated as a human override. The machine is locked (default 120 s) and the new state is adopted. In-game chat commands: `!jawn stop`, `release`, `hold`, `pause`, `resume`. |
| Emergency stop | Triggered from the dashboard, the `STOP` file, chat, Ctrl-C (task cancellation), or an internal crash. It cancels model calls and blocks agent writes. In `restore` mode it returns every machine the harness changed to its pre-harness state, verifies each one, and skips any machine a human changed since. `hold` mode leaves everything as is. Restore is time-bounded and reports anything incomplete. |
| Dashboard | `dashboard/`: loopback-only page. Panels: production chart and targets, constraints and power, machines, an agent card per agent (memory, history, outcomes), the per-action pipeline, conflicts/overrides/messages, alerts, API usage and budget. In simulator mode it also has human and fault controls. POST requests need a per-run token. |
| Extensibility | `control/actions.py` declares action kinds and a capability registry for each backend. Recipe changes, clock changes and switch priority are already declared, and the validator refuses them until a backend implements them. Switch actions are already supported. |
| Tools | `jawn run`, `compare`, `sim-serve`, `capture` (P1 read-only fixture capture and ID discovery), `report`, `deepseek-models`. |

### Simulated factory and FRM

**Factory model:**
- Recipes use game rates: plates 20/min, rods 15/min.
- Inputs are consumed at cycle start; machine buffers hold one stack.
- Storage is slot-limited.
- Belts are rate-limited; supplies stand in for upstream production.
- Circuits can be joined by switches. Overloads trip fuses, which stay tripped until a human resets them; priority switches shed load first.
- The game can be paused, and a reload can change object IDs.
- A truth timeline is recorded for independent checks.

**FRM server:**
- Response shapes follow the FRM 1.5.x docs.
- Reads are unauthenticated; writes need the `X-FRM-Authorization` token. Both `/getX` and `/api/getX` paths work, and CORS is `*`.

**Fault injection:**
- Read errors, delays/timeouts, malformed JSON and dropped connections
- Frozen (stale) data, and objects missing from responses
- The FRM issue #313 behaviour where every endpoint returns 404
- Writes that are ignored, delayed or rejected
- Server down

## 2. Test results

`uv run pytest -q` reports **123 passed in about 41 s**.

| Suite | Tests | Covers |
|---|---|---|
| `test_sim.py` | 10 | recipe rates, pause and resume, starvation and blocking, storage capacity, fuse trip and human reset, priority shedding, switch power cut, game pause, ID rename on reload |
| `test_frm.py` | 14 | response shapes, write auth, unknown IDs, allowlists checked before any I/O, loopback refusal, token kept out of repr, reads retried but writes never retried, timeouts, malformed JSON, 404 everywhere, dropped connections, frozen data, omitted objects, ignored/delayed/rejected writes, simulator control auth |
| `test_observer.py` | 8 | tolerant parsing, unknown values kept as None, carry-over of last good data, each health state (degraded, stale, down, not found, game paused, frozen clock), stale chat never re-delivered |
| `test_control.py` | 13 | every validation code, contention and fairness, occupied slots, pending disables, unknown state counted as running, power headroom and fuses, human-caused violations, override locks, ID re-binding, chat parsing, stop file |
| `test_llm.py` | 24 | decision parsing (13 malformed-output cases), strict schema, cache-stable prompt prefix, pricing peak/off-peak/weekend, budget reservation and caps; DeepSeek paid-call guard, request shape, JSON-mode blank output, error mapping and retries, keep-alive blank lines, https enforcement |
| `test_e2e.py` | 34 | the full experiment with both controllers; storage-measured production; determinism; independent contexts; event-driven wakes; slow-model stale decisions; greedy/stubborn/erratic/invalid agents; power failure; fuse denial; frozen data; flaky reads; ignored, delayed and rejected writes; 404 then recovery; reload ID change; missing target (mid-run, and at start where it aborts without writing); game pause; human override; human exceeding the constraint; stop via chat, file, hold mode, during an FRM outage, and by cancellation; chat hold/resume; budget exhaustion fallback; provider 402; DeepSeek config unable to spend; observe-only |
| `test_app.py` | 8 | dashboard state and token, loopback refusal, **realtime run with a dashboard stop over HTTP**, CLI run and report, refusing live writes without approval, config validation, `capture` against the simulator |
| `test_chaos.py` | 12 (default) | randomised mixes of every fault, human action and agent personality; invariants listed below |

**Chaos soak:** `JAWN_CHAOS_SEEDS=100` passed **100/100**. In every seed the following held:
- the run ended cleanly
- the simulator truth never showed both constraint members running because of the harness
- Dave's smelter was never written to
- no command was left dangling
- no model task was left running
- every write the game received matches a logged harness command

**Matched comparison** (`jawn compare --runs 6`, seeded starting stock, ore rate, buffers and model latency):
- Every run met the targets within 20 minutes, with **0 constraint violations and 0 unverified commands**.
- On seeds where inputs are plentiful the factory is limited by machine speed, so baseline and mock agents finish within about 15 s of each other.
- On the input-starved seed (4), the baseline finished at 890 s and the mock agents at 980 s, using 28 model calls.
- Simulated cost was **$0.0026â€“0.0055 per run**, at peak prices with simulated cache hits.

**Dashboard:** checked three ways:
- Its script passes `node --check`.
- `render()` was exercised against four real state snapshots.
- A headless Edge screenshot of a live realtime run showed all panels populated.

### Defects found and fixed during P0 testing
1. **Safety (fixed):** a constrained machine that disappeared from observations counted as "not running", so the arbiter enabled its peer while it was still running in truth. Unknown state now counts as running. There is a regression test.
2. Chat messages from a failed poll could be re-delivered as new commands in lockstep. Freshness is now tracked for each poll.
3. A per-machine "frozen data" check gave false alarms in steady state. It was removed; frozen data is detected through the game clock instead.
4. Simulator: a grid with no generation tripped its fuse. It is now simply unpowered, as in the game.
5. The summary's maximum-running statistic counted pre-takeover and restore phases. It now counts the main phase only.
6. Changes made during setup triggered the anti-flap dwell timer. Setup changes are now exempt.

## 3. Known limitations (honest list)

- **FRM shapes are unverified.** The simulator's JSON follows the FRM docs, not captured 1.5.6 responses. The parser is tolerant and records problems instead of crashing, but field names such as `InputInventory`, the `setEnabled` response, chat timestamps and `CircuitGroupID` semantics need P1 capture (`jawn capture`).
- **The agents tested so far are heuristics.** The mock "LLM" agents use rules with personalities. Real DeepSeek behaviour is untested and needs P3 spend approval:
  - decision quality, latency and JSON/tool adherence
  - the actual cache-hit rate
  - whether forcing a named function through `tool_choice` works with strict beta
- **Control is limited to pause/unpause and switches** (FRM limitation L1). No recipe or clock changes, construction, or logistics. Fuses cannot be reset remotely; the harness alerts a human instead.
- **Simulator simplifications.**
  - Belts have no transit time and there is no splitter logic.
  - Idle machines draw 0 MW, there is no overclock power curve and no batteries.
  - Productivity is an approximate moving average.
- **Override detection is polling-based.** A human toggle that is reverted within one poll interval (5 s) is not seen. Overrides are detected from the paused flag, not from who made the change (FRM does not say).
- **Ctrl-C** is tested through asyncio task cancellation, which is what `asyncio.run` does on SIGINT. An actual console Ctrl-C on Windows was not exercised automatically.
- **Restore covers machines, not switches.** No experiment controls switches yet.
- **The event store is JSONL only.** The plan's SQLite store was not needed for P0.
- **Game-thread cost is unknown.** Default polling is 6 GETs every 5 s; P1 must measure the frame impact.

## 4. P1 readiness

The harness is ready for a supervised P1 smoke test. Proposed sequence, with every step gated on your approval:

1. **Back up saves:** copy `%LOCALAPPDATA%\FactoryGame\Saved\SaveGames` to a dated folder (copy only).
2. **Install mods:** SMM 3.2.0, then **SML 3.12.1** and **FRM exactly 1.5.6**, in a separate SMM profile so vanilla stays one click away.
3. **Firewall:**
   - Add a rule blocking inbound TCP 8080 from anything except loopback.
   - Confirm the bind address with `netstat -ano | findstr 8080`.
4. **Launch:** create a **new disposable save** and build the test layout (smelter, 3 storage containers, 2 constructors).
5. **Start FRM:** `/frm http start`, then copy `uWS.AuthenticationToken` into the `FRM_TOKEN` user environment variable. It is never committed or logged.
6. **Capture (read-only):** `uv run jawn capture`. This saves raw fixtures, lists machine IDs, and times each endpoint.
7. **Reconcile:** compare the fixtures with the simulator shapes, adjust the parser if needed, and add the fixtures as tests.
8. **Fill in the live config:** copy `experiments/live_plates_and_rods.template.toml` with the real IDs and locations.
9. **Observe-only run:** `uv run jawn run --live --observe-only --duration 300`. Check health, chat, outcome measurement and game-thread impact.
10. **First writes (P2, separate approval):**
    - Run the baseline with `JAWN_ALLOW_LIVE_WRITES=1`.
    - Verify setEnabled readback latency, IDs after save/reload, and every stop path in game.

Open P1 questions (from the plan):
- Is FRM 1.5.6 stable on CL 520140?
- What is the bind address?
- Are IDs stable across a reload?
- What is the setEnabled latency?
- Is chat reliable as a stop channel?
- How much frame time does polling cost?
