# P4 report: power management, more machines and agents, advisories

Status: **done on the simulator**, 2026-11-27. Offline with mock models. No game was launched, no mods were changed and no API spend occurred.

## What was added

| Area | Implementation |
|---|---|
| Power-limited factory | `scenarios/power_grid.toml` (details below) |
| Four independent agents | `experiments/power_grid.toml` (details below) |
| Pessimistic power model | `control/power.py` (details below) |
| Power-aware arbitration | `control/arbiter.py` (details below) |
| Switch priorities (new capability) | Details below |
| Load-shedding attribution | `control/human.py` (details below) |
| Power-safe restore | `runner.py` (details below) |
| Advisories to the human | Details below |
| Views, baseline, mock | Details below |
| Report and dashboard | Details below |

### Power-limited factory
`scenarios/power_grid.toml`:
- **Supply:** a 20 MW plant feeds an iron line and a copper line through priority switches.
- **Backup:** a 10 MW coal generator connects through a plain switch. It holds 900 s of fuel and burns fuel only while it supplies load (`fuel_s` is new in the simulator).
- **Load:**
  - Dave's two unmanaged smelters plus four managed constructors draw 24 MW.
  - Without the backup, only two of the four managed constructors can run at once.

### Four independent agents
`experiments/power_grid.toml`:
- `plate_agent` and `rod_agent` each own one machine.
- `copper_agent` owns a two-machine chain: wire feeding cable over a direct belt.
- `power_agent` owns the three switches.
  - Its objective is to keep the lines powered, use backup fuel only when needed, and restore lines that were shed.
- No slot rule couples the agents; power does.

### Pessimistic power model
`control/power.py`:
- **Demand:** every machine not known to be paused counts at full power, as do pending enables.
- **Switch topology:** FRM reports power per circuit group only, so the harness learns each switch's two sides while it is open: capacity and machine IDs per side.
- **Classification:** switches are classed as a `supply_tie`, a `line_tie` or unknown.

### Power-aware arbitration
`control/arbiter.py`:
- **Enables:** projected against pessimistic demand. Enabling on a grid with no generation is allowed, because it draws nothing until the switch closes.
- **Closing a switch:** the merged demand must fit the merged capacity.
- **Opening a switch:** each side that keeps generation must carry its own learned load, using current (not stale) capacity estimates. A switch never observed open cannot be opened (`POWER_TOPOLOGY_UNKNOWN`).
- **Pending changes:** while a switch change is in flight, all load increases wait (`POWER_PENDING`).

### Switch priorities (new capability)
- **Evidence:** the FRM 1.5.6 DLL calls `AFGBuildablePriorityPowerSwitch::SetPriority`, and `setSwitches` reports "Missing field priority, name or status."
- **Action:** `switch.set_priority` is a value-carrying action.
  - The decision tool gets an integer `priority` field, but only for agents allowed to use it.
  - The validator requires a value from 1 to 8 and a real priority switch.
- **Verification:** readback checks the switch's `Priority`.
- **Restore:** stop/restore returns each priority to its value at takeover.
- **Recipes and clock speeds:** the DLL has read-only `getRecipes` and no setters, so these stay unsupported.

### Load-shedding attribution
`control/human.py`:
- A priority switch that opens by itself is logged as `power_shed`, not as a human override.
- It gets a short lock (`shed_lock_s`, default 30 s) instead of the 120 s human lock.
- FRM cannot say who opened a switch, so a human opening a priority switch is labelled the same way.

### Power-safe restore
`runner.py`. The restore runs in this order:
1. Machine disables.
2. Switch closes.
3. Machine enables, each power-checked.
4. Switch opens, each checked against the learned topology.
5. Switch priorities.

- Any step that could overload a grid is skipped and reported to the human.
- Setup is checked the same way.
- After the stages, the harness waits for in-flight commands to resolve, without sending new ones.

### Advisories to the human
- An agent can set `message_to: "human"` for things only the player can do, such as resetting a fuse.
- Advisories are rate-limited per agent (`advisory_min_interval_s`) and never repeated.
- They appear in alerts, the dashboard and the report, and in game chat when `announce_in_chat` is on.

### Views, baseline, mock
- **Views:** agents see their switches (state, kind, supply MW) and grid potential. Machines on a dead grid show as `unpowered`, and peer roles are listed in the prompt.
- **Baseline:** deterministic switch rules:
  - restore a shed line when it fits;
  - close the backup when demand exceeds capacity;
  - open it when everything fits with 10% hysteresis.
  It is also the budget fallback.
- **Mock:**
  - a `power` personality;
  - agents that own several machines;
  - machine agents that ask the power manager for power after a `POWER_HEADROOM` denial.
- **Intermediate machines:** a machine without its own product, such as wire, counts as done once its owner's targets are met.

### Report and dashboard
- **Report:** a new `power` section with switch on-time in the main phase, sheds, fuse trips, power denials and advisories.
- **Dashboard:** switches with their learned kind, potential demand per circuit, and load-shed and advisory events.

## Results

`uv run pytest -q`: **150 passed** (about 60 s). Of these, 24 are new power tests in `tests/test_power.py`.

The power tests check safety from **simulator truth** (`PowerTruth`):
- Every harness write that adds load must leave the grid's pessimistic demand within its capacity.
- Every load shed or fuse trip must follow an external event: a generator change, a human action, or fuel running out.

| Test | Shows |
|---|---|
| nominal (baseline, agents) | Targets met, 0 unverified, **0 sheds and 0 fuse trips including the restore**, backup used then taken out, Dave's state restored |
| cooperation | Power denial leads to a message to the power agent, which then switches the backup on |
| generator failure (baseline, agents) | Game sheds the iron line. Logged as `power_shed`, not a human override; backup brought in; line restored once it fits; targets met |
| fuse trip | No priority switches, so the fuse trips. Nothing is enabled on the tripped grid; exactly one advisory to the human; recovery after the human reset |
| refused switch-off | Opening the backup under full load gives `POWER_HEADROOM`; opening a never-observed switch gives `POWER_TOPOLOGY_UNKNOWN`; truth shows no shed |
| priorities | Invalid value 9 rejected at parse time; plain switch gives `NOT_PRIORITY_SWITCH`; copper set to shed first, so the game sheds copper, not iron; original priorities restored |
| restore when Dave's own state overloads | Unsafe re-enables skipped and reported; no overload |
| stop mid-run, budget fallback, backup out of fuel, dashboard and report, config validation | Each as named |

**Chaos soak, 100 seeds each, 200/200 passed.** `JAWN_CHAOS_SEEDS=100` ran both the power chaos and the original chaos.
- Faults mixed in: generator drops, human switch and machine toggles, read errors, frozen data, delayed and ignored writes, and reloads with ID changes.
- Agent personalities were mixed.
- In every seed:
  - no harness write overcommitted a grid;
  - no unexplained shed or fuse trip occurred;
  - Dave's smelters were never written to;
  - no command was left dangling;
  - every game write matched a logged command.

**Matched comparison** (`jawn compare --config experiments/power_grid.toml --runs 6`, mock models):

| | Baseline | Mock agents |
|---|---|---|
| Targets met | 6/6 | 6/6 |
| Violations / unverified | 0 / 0 | 0 / 0 |
| Finish (s, typical seed) | 347 | 402–422 |
| Backup on-time (s) | 180 | 215–240 |
| Load sheds | 0 | 0 |
| Model calls per run | 0 | 52–66 (simulated about $0.012) |

The deterministic power rules beat the mock power manager:
- The baseline brings the backup in as soon as it sees waiting demand.
- The mock waits for a request, then needs two calm checks before switching the backup off.

The mock exists to exercise the protocol, not to optimise; a real model is the open question.

## Defects found and fixed during P4

1. **The restore could overload the plant.** At the end of a run it re-enabled every machine at once. The game then shed a line.
   - Restore is now staged and power-checked.
   - The scenario's own "Dave state" was also made feasible.
2. **Stale learned capacity.** The check for opening a switch used each side's capacity as learned at setup. After a generator failure, the restore switched the backup off onto a grid that could no longer carry the load. The truth monitor flagged this as an unexplained shed.
   - Fix: side capacity is now bounded by the current merged capacity minus the other side's share.
3. **Dangling restore command under ignored writes.** The staged restore could end with a retry still unverified.
   - Fix: the runner now waits, bounded by the verify timeout, for in-flight commands to resolve.
4. **Switches never had a stale-state check.** Their observed state was not recorded with the decision, so the `STATE_CHANGED` check never applied to them.
   - Fix: the controller now records the observed state of every owned target, which enables `STATE_CHANGED` for switches.

## Limitations

- **Live behaviour unverified.** FRM's switch fields have not been captured live, so the simulator's assumptions are unconfirmed:
  - `Connected0/1` semantics;
  - whether `CircuitGroupID` equals the circuit ID of a single-circuit side;
  - the setSwitches priority body.
  P1 capture must confirm them.
- **Topology learning needs each switch observed open once.** Until then, opening it is refused (fail-safe).
- **Shedding cannot be told apart from a human.** A human opening a priority switch is labelled `power_shed` (shorter lock). Human priority changes are not detected as overrides.
- **Simplified generators:** a generator is online or offline with no fuel logistics. Machines draw full power only while producing, with no idle draw and no overclock curve.
- **Mock power policy is naive** and does not use priorities. Priority control was tested with scripted decisions. A real-model P4 run would need spend approval.
- **Recipe and clock control remain impossible** with FRM 1.5.6.
