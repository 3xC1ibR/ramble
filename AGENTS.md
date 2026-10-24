# Agent notes for ramble

Read `README.md` first; it has the architecture, data layout, settings and code map.

- **Runtime data lives in `~/.ramble`; never write it into the repo.** To debug a decision, look at `ramble show <id>` and `~/.ramble/triage/<id>.json`, which hold the exact triage input and Claude output.
- **Test against a throwaway home and spawn no agents:** `RAMBLE_HOME=/tmp/ramble-test` with `{"dispatch_daily_cap": 0}` in its `config.json`, a `ramble run` in that environment, then `ramble say "..."` or `ramble inject "..."`. Don't run a second daemon against `~/.ramble` while the app is running.
- **After code changes, restart the installed app** with `launchctl kickstart -k gui/$(id -u)/local.ramble`. If you change `macos/` or `launchd/`, re-run `./bin/install`.
- **Don't open the mic from a terminal or agent shell to test.** Those processes have no mic permission and get silent all-zero audio. Only `Ramble.app` (and its Python child) holds the permission.
- **Triage runs use `--settings '{"disableAllHooks": true}'`** so they don't register as Ribbon Field threads.
- **Dispatches need a caller session**, so they use `--from-session` with ramble's own registered session (`~/.ramble/agent_coord_session`, name "Ramble voice dispatcher").
- **Behavior changes usually belong in `prompts/triage.md`**, not code. Keep the structured decision schema in `ramble/router.py` in sync with the prompt.
[historical text removed for privacy]
