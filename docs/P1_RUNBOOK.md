# P1 runbook: live smoke test (read-only)

Status, 2026-11-27: offline preparation is done. The remaining steps modify the game folder,
the firewall, or need someone at the keyboard in game, so they wait for the owner.

## Done offline

| Step | Result |
|---|---|
| Save backup | `%LOCALAPPDATA%\FactoryGame\Saved\SaveGames` copied to `%USERPROFILE%\Documents\FactoryGame-SaveBackup-2026-11-27`. All 15 files' SHA-256 hashes match the originals. Nothing in the original folder was changed. |
| Mod packages | Pinned on api.ficsit.app. SML 3.12.1 (`game_version >=520140`) and FRM 1.5.6 (`>=520140`, depends on `SML ^3.12.1`), Windows targets. Both downloads match the published SHA-256. |
| FRM field names | Strings extracted from the shipped FRM 1.5.6 DLL. Every JSON key the observer parses is present, including `InputInventory`, `CircuitGroupID`, `IsPaused`, `IsProducing`, `FuseTriggered` and `ServerTimeStamp`. The write body keys `ID` and `status` are present too. So are all eleven endpoints the harness uses, read and write. The only simulator key not in the DLL is `PowerProductionCapacity` in the simulated `getGenerators`, which the harness does not read. Nesting and value types still need `jawn capture`. |
| Bind address | FRM has settings `uWS.Port`, `uWS.Autostart`, `uWS.AuthenticationToken`, `uWS.Root`, `uWS.PushCycle`, but **no bind-address setting**. uWebSockets therefore listens on all interfaces, so the firewall rule below is required, not optional. |
| Install script | `scripts/p1_mods.ps1`: verifies SHA-256 and writes only to `<game>\FactoryGame\Mods`. It refuses if the game is running or Mods already exists. `-Uninstall` removes Mods only if it holds just these two mods; `-Check` reports. Tested end to end on a fake game folder. The real game is still vanilla. |
| Firewall script | `scripts/p1_firewall.ps1`: an inbound block rule on TCP 8080, all profiles. `-Remove` and `-Check` are available, and it refuses to run without admin. |

## Remaining steps (owner)

1. **Install mods** (game closed):
   `powershell -ExecutionPolicy Bypass -File scripts\p1_mods.ps1`
   To go back to vanilla at any time: `... -Uninstall`, or Steam > Verify integrity (it does not delete Mods; use the script).
2. **Firewall** (Administrator PowerShell):
   `powershell -ExecutionPolicy Bypass -File scripts\p1_firewall.ps1`
   If Windows asks to allow the game through the firewall, choose **Cancel**; the block rule wins either way.
3. **Launch** Satisfactory through Steam and check the main menu shows mods loaded (SML adds a Mods button).
   **Create a new save** (do not load an existing world) and build: one Smelter fed with ore,
   storage for ingots, two Constructors (Iron Plate, Iron Rod) fed from the ingot storage, a storage
   container after each, and power.
4. **Start FRM:** type `/frm http start` in chat, or set Autostart in Options > Mods > Ficsit Remote Monitoring.
   Copy the authentication token from the same settings page into a user environment variable:
   `setx FRM_TOKEN "<token>"`, then open a new terminal. It is never written to the repo or logs.
5. **Check binding:** `powershell -File scripts\p1_firewall.ps1 -Check` should show the rule and a listener on 8080.
6. **Capture (read-only):** `uv run jawn capture --url http://127.0.0.1:8080`. This saves raw JSON to `fixtures/live/` (gitignored), lists machine IDs and recipes, and times each endpoint.
7. Tell Claude when capture is done. Next: reconcile the parser, fill `experiments/live_plates_and_rods.toml`, then run `uv run jawn run --live --observe-only --duration 300`.

Writes (P2) need a separate go-ahead and `JAWN_ALLOW_LIVE_WRITES=1`. Model spend (P3) needs a
separate budget approval.
