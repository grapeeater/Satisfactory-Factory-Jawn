# Satisfactory Factory Jawn — Findings & Implementation Plan

Status: **proposal, not yet approved.** Prepared 2026-11-26. Nothing has been installed, launched, or spent.

## 1. Local environment (verified on this PC)

| Item | Finding |
|---|---|
| Install | Steam, `C:\Program Files (x86)\Steam\steamapps\common\Satisfactory` (appid 526870) |
| Build | `++FactoryGame+rel-main-1.2.6-CL-520140`, UE 5.6.1, `.version` GameVersion `1.2.5.1`, Steam buildid 26728002, default (stable) branch |
| Meaning | This is **Patch 1.2.5.1 / CL 520140** (2026-11-18). "rel-main-1.2.6" is an internal branch name. Stable and experimental are currently the same build. |
| Mods | None. No `FactoryGame\Mods`, no SMM installed. |
| Saves | 14 `.sav` files under `%LOCALAPPDATA%\FactoryGame\Saved\SaveGames\76561198075588108\`, last written March 2026. These are untouched and must stay that way. |
| Toolchain | Python 3.13.14, uv, Node 24.16, git. No `gh` and no Docker (neither is needed). |
| Keys | `DEEPSEEK_API_KEY` and `ANTHROPIC_API_KEY` are present as environment variables (only checked for presence; not read). |
| Hardware | Ryzen 7 7700X, 31 GB RAM, RX 9060 XT. Enough to run the game and the harness on one PC. |

## 2. Compatibility research (2026-11-26; registry and docs claims, not local tests)

| Component | Version | Status for CL 520140 | Notes |
|---|---|---|---|
| SML | 3.12.1 (2026-11-21) | Registry: Works on stable and experimental, `>=520140` | Adds 1.2.5.1 support and a multiplayer join crash fix. |
| SMM | 3.2.0 (2026-11-08) | Current | Profiles and the "Mods off" toggle give a vanilla rollback. |
| **FRM** | **1.5.6** (2026-11-22) | Registry: Works, `>=520140`, requires SML ^3.12.1 | **Mandatory version.** 1.5.5 crashed on this build (issue #330: access violations in getSessionInfo, getSmelter and getFactory). The fix in 1.5.6 is claimed; nobody has confirmed it yet. |
| FicsIt-Networks | 1.2.0 (2025-09) | **Broken** ("Broken by the update to Unreal Engine 5.6.1") | Port in progress on the dev branch, no release. Not usable. |
| Official dedicated-server API | n/a | Server management only | No factory-machine access. |
| Save parsers | `@etothepii/satisfactory-file-parser` 4.1.2 (claims U1.2), `sat_sav_parse` (to 1.2.2.1) | Unknown for 1.2.5.1 | Test-parse a **copy** of a save before relying on it. |

### What FRM 1.5.6 can actually do

Read (most useful):
- `getFactory` and the per-type endpoints, with fields ID, ClassName, location, Recipe, production[] {CurrentProd, MaxProd, ProdPercent}, ingredients[], Input/OutputInventory, ManuSpeed, IsConfigured, IsProducing, IsPaused, PowerInfo
- `getStorageInv`, `getWorldInv`, `getPower`, `getSwitches`, `getProdStats`, `getSessionInfo`, `getChatMessages`

Write (POST, `X-FRM-Authorization` token, run on the game thread):
- `setEnabled`: pause or unpause any factory building or circuit switch
- `setSwitches`: on/off, name, and priority 0–8
- `sendChatMessage`
- `createPing`
- `setModSetting`

**It cannot set recipes, clock speed, shards or somersloops, change belts or splitters, or build or dismantle anything.**

Server behaviour:
- HTTP and WebSocket on port 8080. Autostart is off by default (`/frm http start`, or `uWS.Autostart`).
- Config lives in `GameUserSettings.ini` under `FicsitRemoteMonitoring.uWS.*`.
- **Reads are unauthenticated.** The server sends `Access-Control-Allow-Origin: *` and probably binds all interfaces.
- Most endpoints are marshalled onto the game thread, so polling costs frame time.

### DeepSeek API (current)

- `deepseek-chat` and `deepseek-reasoner` were scheduled for retirement on 2026-07-24. Current models are `deepseek-flash` (V4.1-Flash) and `deepseek-v4-pro`.
- **Thinking is on by default.** Disable it for cheap decisions: `thinking: {type: disabled}`.
- Flash pricing per 1M tokens, off-peak / peak:
  - cache hit $0.003 / $0.006
  - cache miss $0.15 / $0.30
  - output $0.60 / $1.20
- Peak is weekdays 01–04 and 06–10 UTC.
- The KV cache is automatic, prefix-only and best-effort. Usage reports `prompt_cache_hit_tokens` and `prompt_cache_miss_tokens`.
- Strict tool calling is in beta (`/beta`). It supports `enum` and needs every field marked required.
- JSON mode can return blank content; the harness must handle that and retry.
- `GET /user/balance` exists.

## 3. Limitations and how the plan handles them

| # | Limitation | Proposed solution |
|---|---|---|
| L1 | Agents can only pause/unpause machines and set switches. No recipe, clock or construction control. | The action space is enable / disable / wait, plus switch toggles. Agents can also file **advisories** (for example "suggest recipe X on machine Y"), which go to you via in-game chat and the event log. Long-term option: contribute `setRecipe` and `setPotential` write endpoints upstream to FRM (open-source C++), or wait for the FicsIt-Networks port. Both are deferred. |
| L2 | FRM 1.5.6 stability on CL 520140 is unconfirmed, and there is a history of null-pointer crashes. | Phase 1 smoke test on a disposable save. Every endpoint is exercised once with a crash watchdog. The harness uses an **allowlist of endpoints proven locally**. Known crashers are excluded by default: getTrainRails, getPlayer, getFactoryCart, getSchematics, getRecipes, getTradingPost, getSpaceElevator. |
| L3 | Reads have no auth, CORS is `*`, and the server likely binds 0.0.0.0. | A Windows Firewall rule blocks inbound TCP 8080 except loopback. Verify with `netstat`. The harness talks to `127.0.0.1` only. The token is read from an env var or the local ini, never committed, and redacted in logs. |
| L4 | Game-thread cost of polling. | **One observer process** is the only reader of FRM. It sends a few calls per cycle (default 5–10 s, adaptive), and agents read cached snapshots, never FRM. Phase 1 measures endpoint latency and frame impact. |
| L5 | HTTP 200 doesn't prove anything happened. Writes and reads share the same mod. | Every action has three separate checks: the **API response**; a **readback** of IsPaused/IsProducing on the next poll(s) within a timeout; and an **outcome measurement**, which is storage-container item deltas and production rates over time. Optional end-of-run check: parse a copied save with an independent parser (once proven on 1.2.5.1). |
| L6 | Endpoint 404 after a session reload (#313), and a port change ignored (#329). | A health monitor detects 404s and timeouts, freezes all agents (no actions on stale state), and alerts you. Documented procedure: restart the game, and don't change the port while the server is running. |
| L7 | Machine IDs may not be stable across save/load. | Verify in Phase 1. If they aren't stable, map machines by ClassName + location (and you name them). |
| L8 | Human priority: you play live and may toggle the same machines. | Any state change the harness didn't cause counts as a **human override**. The machine is locked for N minutes and the event is logged. Stop controls: a `STOP` file, Ctrl-C, and an in-game chat command (`!jawn stop` / `!jawn pause`, read through getChatMessages). If chat proves unreliable, the stop file is the fallback. |
| L9 | LLM latency and outages. | Model calls run on async tasks with timeouts and never block observation. A failed call means **wait** (safe default). Every decision carries a snapshot sequence number and is rejected if stale. |
| L10 | API spend. | Agents are **event-driven, not per-tick** (deterministic wake triggers), with a stable cached prompt prefix, flash with thinking off, ~150 max output tokens, strict tool schema, and local cost accounting from usage fields with a hard per-run cap. Offline development uses mock and replay providers (zero spend). |
| L11 | "Independent" agents could collapse into one model narrating. | Each agent has its own process context: objective, owned machines, memory file and message history. Agents never see each other's reasoning, only each other's **messages** and the shared world. Arbitration is **deterministic code**, not an LLM. Independence and quality are measured against a rule-based baseline. |
| L12 | Single PC: harness and game compete for CPU. | The harness is tiny (Python asyncio, I/O-bound). Observer cadence is capped. |

## 4. Architecture

```
FRM (127.0.0.1:8080)
   │  allowlisted GETs only
   ▼
Observer ──► Snapshot store (SQLite, seq#, timestamps, derived rates) ──► Health monitor
   │                                   │                                     │ (freeze on stale/404)
   │                    per-agent filtered view
   │                                   ▼
   │      Agent A ─┐   Agent B ─┐   (own objective, memory, wake triggers, LLM adapter)
   │               ▼            ▼
   │           Proposals (schema-validated, pydantic)
   │                       ▼
   │           Validator: authority (owns machine), allowlist, staleness,
   │                      min-dwell/hysteresis, human locks, budget
   │                       ▼
   │           Arbiter (deterministic): human > safety > constraints > need-based priority
   │                       ▼
   │           Executor (single writer, serialized POST setEnabled)
   │                       ▼
   └────────── Readback verifier (next polls) ──► Outcome meter (storage deltas)
                                 ▼
               Event log (JSONL + SQLite): decision | validation | arbitration |
               request | response | readback | outcome | human-override | cost
```

Modules (Python 3.13, uv, asyncio, httpx, pydantic v2, sqlite3, rich for the console view):

- `frm/`: typed client with timeouts, retries on GET only, endpoint allowlist, and token handling. Also a **mock FRM server** that simulates constructors, inventories and IsPaused, so the whole harness can be developed and tested offline.
- `observe/`: observer loop, snapshot normalization, derived production rates, health.
- `agents/`: the Agent runtime, which handles wake triggers, prompt assembly (stable prefix, then dynamic suffix), a bounded memory note written by the agent itself, and inter-agent messages.
- `llm/`: provider-neutral interface with adapters for DeepSeek, Mock (scripted), Replay (recorded responses), and later Claude/OpenAI/local. It includes the budget meter.
- `control/`: validator, arbiter, executor, verifier, human-override detector, stop controls.
- `baseline/`: deterministic rule-based controller using the same interface as the agents.
- `eval/`: run report covering items produced versus target, time, actions proposed / accepted / verified / failed, conflicts, overrides, tokens, cache-hit ratio and cost.
- `config/`: per-experiment YAML (machines, owners, targets, constraints, budget, model).

Agent decision contract (strict tool call):

```json
{"action": "enable|disable|wait", "machine_id": "<owned id>", "reason": "<=200 chars",
 "message_to": "none|<agent id>", "message": "<=200 chars", "memory_update": "<=300 chars"}
```

## 5. Phases and gates

| Phase | Work | Needs your approval? |
|---|---|---|
| **P0 Offline foundation** | Repo scaffold, FRM client, mock FRM simulator, observer, validator, arbiter, executor, verifier, event log, baseline controller, mock and replay LLM, unit and integration tests. The first experiment runs end-to-end **against the simulator**. | No. Local code only, no game, no spend. |
| **P1 Live smoke test** | Back up the SaveGames folder (copy only). Install SMM → SML 3.12.1 + FRM 1.5.6. Firewall rule. Launch, create a **new disposable save** (Advanced Game Settings / creative-style), start FRM. Probe each endpoint once, capture real fixtures, verify IDs, setEnabled readback latency, token, binding, and behaviour after save/reload. | **Yes**: install mods, firewall rule, launch game. |
| **P2 Baseline live** | Build the 2-constructor test factory (iron plate + rod, buffered inputs, output storage) and run the **rule-based controller** for 20-minute matched runs. Shows that the action/readback/outcome loop works with no LLM. | Yes: launch game. Still no spend. |
| **P3 LLM agents live** | Two DeepSeek agents in the same scenario, over several matched runs compared with the baseline. Explicit model (`deepseek-flash`, thinking off), hard cap (proposed **$1.00 total**; expected well under $0.10), per-run stop conditions, actual usage report. | **Yes**: API spend. |
| P4 Extensions | Power-manager agent (switches and priorities), more machines and agents, advisories, then cautiously your real world. Investigate an upstream FRM PR for recipe and clock. | Each step gated separately. |

### First experiment (from HANDOFF, made concrete)

- **Machines:** one constructor making iron plates (agent "Plate") and one making iron rods (agent "Rod").
- **Arbiter rule:** at most one machine running at a time. Your actions take priority.
- **Target:** at least 80 of each in 20 minutes. It is feasible:
  - Plates at 20/min need 4 minutes; rods at 15/min need about 5.3 minutes.
  - That totals about 9.3 minutes of the 20 with one machine running at a time, which leaves room for switching costs and mistakes.
- **Measured:** storage counts (start/end, sampled over time), machine state timeline, accepted / rejected / unverified actions, conflicts, human interventions, and per-call tokens and cost.
- **Comparison:** the baseline runs N=3 or more matched runs from the same starting save (reload a copy of the test save each run).
- **Cost estimate:** each agent wakes on events (target reached, input starved, output full, permission granted or denied, peer message), with a floor of one call per 60 s.
  - That is at most about 40 calls per run, at roughly $0.0001 each with cache hits, so **under $0.01 per run** (estimate; actual usage will be logged).

## 6. Open questions to settle in P1

- Is FRM 1.5.6 stable on CL 520140 for the endpoints we need?
- Which endpoints are safe at which polling rates?
- What is the actual bind interface?
- Are IDs stable across reloads?
- What is the latency from setEnabled to IsPaused readback?
- Is `getChatMessages` reliable for a stop command?
- Does setEnabled work on constructors in a single-player host session?
- Can either save parser read a 1.2.5.1 save copy?
