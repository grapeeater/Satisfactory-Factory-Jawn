# P3 (simulator): real DeepSeek agents vs the baseline

Status: **done on the simulator**, 2026-11-27. Live-game P2/P3 are deferred, since the owner skipped
game testing for now.

- Model: `deepseek-flash`, with thinking disabled, strict tool calling, and temperature 0.
- Factory: the simulated plates-and-rods factory.
- Approved cap: **$0.25**. Actual total spend: **$0.0325** (156 calls).
- All calls were made off-peak (Fri ~15:00–16:00 UTC).

## Method

Command: `jawn compare --provider deepseek --realtime --speed 4 --seeds 1,2,4`.
- **Matched runs:** each seed applies the same perturbation of starting stock, ore rate and buffers to both controllers.
- **Seed 4** is input-starved.
- **Clock:** wall-clock time at 4× sim speed. The real model latency of about 1.2 s therefore counts as about 5 game-seconds, which makes timing harsher than in the live game.
- **Spend limits:** a $0.06 per-run cap, plus a comparison-wide cap that clips every run's budget.
- **Lockstep is refused:** a paid provider cannot run on the deterministic lockstep clock, because real latency would land hundreds of game-seconds late. A test enforces this.

## Results

**Round 2** (final prompt). "Done at" is the reached-after time of the second target.

| Seed | Controller | Plates | Rods | Done at (s) | Run ended (s) | Commands (verified) | Conflicts | Violations | Calls | Invalid | Cost |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | baseline | 80 | 81 | 572 | ~589 | 4 (4) | 0 | 0 | 0 | 0 | $0 |
| 1 | DeepSeek | 84 | 83 | 597 | 625 | 4 (4) | 1 | 0 | 20 | 1 | $0.0043 |
| 2 | baseline | 80 | 81 | 572 | ~589 | 4 (4) | 0 | 0 | 0 | 0 | $0 |
| 2 | DeepSeek | 86 | 83 | 592 | 620 | 4 (4) | 1 | 0 | 18 | 2 | $0.0037 |
| 4 (starved) | baseline | 82 | 80 | 891 | ~905 | 8 (8) | 0 | 0 | 0 | 0 | $0 |
| 4 (starved) | DeepSeek | 82 | 85 | **593** | 615 | 4 (4) | 1 | 0 | 28 | 2 | $0.0060 |

- **Safety:** 0 harness constraint violations and 0 unverified commands in every run. Each "conflict" is the arbiter correctly denying a request for the occupied slot.
- **Speed:** with ample input the agents finish about 20–25 s behind the baseline, which is the cost of model latency. **On the starved seed they finished about 300 s faster.** They ran rods first while plates were short of ingots, rather than following the baseline's fixed order.
- **Model behaviour:**
  - Latency: median 1.2 s, max 2.5 s.
  - Output: about 178 tokens per call.
  - Cache: about 48% of prompt tokens were cache hits. The shared system and role prefix is about 640 tokens.
  - Cost: about $0.0002 per call, and $0.004–0.006 per run.
- **Coordination:**
  - Every run negotiated in plain messages ("will yield when target met") and kept those promises.
  - In each run there was one denied enable request, and it was not retried while the situation was unchanged.

## Defects found and fixed

1. **Output truncation.** At `max_tokens = 200`, 3 of the first 11 responses hit the limit (`finish_reason=length`), so their JSON was cut off. The limit is now 400, and the prompt asks for shorter text: reason and message under 120 characters, memory under 200. No truncation occurred in the 145 calls after the change (max 216 tokens).
2. **Overproduction from an under-specified objective.** In round 1, the agents reached 80 rods and then left the machine running ("objective complete, no need to touch machines"). They delivered up to 234 rods, consumed shared ingots, and each run went the full 1200 s. A general rule was added to the agent prompt: output beyond the target is waste, so disable the machine once the target is met. In round 2 every agent disabled its machine and the runs ended about 10 minutes earlier.
3. **Strict mode is not strictly enforced by DeepSeek.** 6 of 145 compare responses named the field `memory` instead of `memory_update`. The parser correctly rejected them; each lost decision was a harmless `wait`. The parser now accepts that exact alias, but only when the real key is absent. All other unknown keys are still rejected. A regression test uses the real response shape. Replaying all 156 real responses, 154 now parse; the 2 that don't are the truncated ones from fix 1.

## Spend

| Run set | Calls | Cost |
|---|---|---|
| Smoke test (90 s) | 11 | $0.0023 |
| Round 1 (3 seeds) | 79 | $0.0163 |
| Round 2 (3 seeds) | 66 | $0.0140 |
| **Total** | **156** | **$0.0325** |

## Remaining limitations

- The results come from the simulator, not the game. Live P2/P3 still need the in-game setup in `docs/P1_RUNBOOK.md`.
- The sample is small: 3 seeds × 1 run each, at temperature 0. That is not enough for statistical claims.
- The runs used 4× sim speed. At 1× speed, latency matters four times less.
- The cache hit rate is capped by prompt structure. About half of each prompt is the changing observation; shortening the observation JSON would cut cost further.
