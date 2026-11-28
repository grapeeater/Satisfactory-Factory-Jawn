# Getting a DeepSeek API key

The AI agents run on DeepSeek's API. Everything except real agents works without a key:
- the simulator;
- the mock agents;
- the rule-based baseline.

You only need a key to run real agents. They are cheap: the P3 test made 156 model calls for about $0.03 in total.

## 1. Make an account and add credit

1. Go to <https://platform.deepseek.com> and sign up.
2. Open **Top up** and add a small amount, such as the minimum.
   - DeepSeek is prepaid, so you can never be billed more than the balance you put in.
   - With no balance, calls fail with "insufficient balance".

## 2. Create a key

1. Open **API keys** → **Create new API key**, and name it something like `factory-jawn`.
2. Copy the key, which starts with `sk-`. **It is shown only once.** If you lose it, delete it and make a new one.

Treat the key like a password:
- Don't paste it into chat, a config file or anything in this repo.
- If it leaks, delete it on the API keys page and create a new one.

## 3. Store it as an environment variable (Windows)

In PowerShell, replace `sk-your-key-here` with your key and run:

```powershell
[Environment]::SetEnvironmentVariable('DEEPSEEK_API_KEY', 'sk-your-key-here', 'User')
```

Close and reopen your terminal (or editor) so it picks up the variable. Then check the key works. This call is free and uses no tokens:

```powershell
uv run jawn deepseek-models
```

It should print the model names. If it prints `DEEPSEEK_API_KEY not set`, the terminal is still the old one.

## 4. Run real agents (on the simulator first)

Paid calls are off by default. A run spends money only when **both** of these are true:
- the experiment config sets `allow_paid = true` and a non-zero `budget.max_usd`;
- you set `JAWN_ALLOW_PAID_CALLS=1` in that terminal.

Every call reserves its worst-case cost first, so a run stops before it passes its cap.

```powershell
$env:JAWN_ALLOW_PAID_CALLS = 1
uv run jawn compare --config experiments/sim_deepseek_plates_and_rods.toml --provider deepseek --realtime --speed 4 --runs 3 --max-total-usd 0.25
```

- Real models need `--realtime`. The harness refuses to run them in the simulator's fast lockstep mode.
- `--max-total-usd` is a hard cap across all the runs.
- Each run's report shows its actual cost in `runs/<run-id>/`.

To turn paid calls off again, close the terminal or run `Remove-Item Env:JAWN_ALLOW_PAID_CALLS`.

## Changing or removing the key

- **To change it:** run step 3 again with the new key, then delete the old key on DeepSeek's API keys page.
- **To remove it from this PC:**

  ```powershell
  [Environment]::SetEnvironmentVariable('DEEPSEEK_API_KEY', $null, 'User')
  ```
