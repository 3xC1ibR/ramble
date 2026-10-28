# Agent notes for ramble

Read `README.md` first; it has the architecture, data layout, settings and code map.

## Companion experience

Voice carries the conversation; Companion shows the material the user needs to
understand and steer the work. Treat the active session and its visual results as
one workspace. For “show me how FooBar works,” show actual code and useful
diagrams while explaining briefly aloud. Publish useful visuals during work,
not only at the final answer. Keep the visual visible through spoken progress;
conversation text belongs in secondary history. Follow new visuals live unless
the user explicitly holds an artifact or opens history. Keep runtime guidance in
`prompts/work-session.md`, `prompts/fast.md` and `prompts/triage.md` consistent.
Companion lives in the sibling `ramble-ipad-display` project; preserve automatic
updates and reconnect recovery.

## Development rules

- **Runtime data lives in `~/.ramble`; never write it into the repo.** To debug a decision, look at `ramble show <id>` and `~/.ramble/triage/<id>.json`, which hold the exact triage input and Claude output.
- **Test against a throwaway home and spawn no agents:** `RAMBLE_HOME=/tmp/ramble-test` with `{"dispatch_daily_cap": 0}` in its `config.json`, a `ramble run` in that environment, then `ramble say "..."` or `ramble inject "..."`. Don't run a second daemon against `~/.ramble` while the app is running.
- **After code changes, restart the installed app** with `launchctl kickstart -k gui/$(id -u)/local.ramble`. If you change `macos/` or `launchd/`, re-run `./bin/install`.
- **Don't open the mic from a terminal or agent shell to test.** Those processes have no mic permission and get silent all-zero audio. Only `Ramble.app` (and its Python child) holds the permission.
- **Triage runs must not register as Ribbon Field threads.** Codex triage uses `--ignore-user-config --disable hooks --ephemeral` (without them it loads the agent-coord plugin and creates a "Ramble thought triage" thread). Claude triage uses `--settings '{"disableAllHooks": true}'`.
- **The decision schema must stay compatible with OpenAI strict structured outputs:** every property is listed in `required`, optional values are nullable, and `additionalProperties` is false.
- **Dispatches need a caller session**, so they use `--from-session` with ramble's own registered session (`~/.ramble/agent_coord_session`, name "Ramble voice dispatcher").
- **Behavior changes usually belong in `prompts/triage.md`**, not code. Keep the structured decision schema in `ramble/router.py` in sync with the prompt.
- **The user controls the listening switch.** Don't change `state.json` except in throwaway test homes.
- **Never play test audio through the speakers.** The live app hears it and acts on it; a fixture once dispatched two real agents. Generate files with `ramble say --no-play --output f.wav "..."` and feed them to a throwaway daemon with `ramble ingest f.wav`. Set `"fast_speak": false` and `"speak_questions": false` in the test config.
