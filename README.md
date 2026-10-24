# ramble

[historical text removed for privacy]

```
mic ─► speech detection ─► FLAC segments ─► local Whisper ─► thoughts ─► triage agent ─► Ribbon Field threads
       (Silero VAD)        (~/.ramble/audio)   (MLX, ~1s)     (grouped)    (Codex, Astra)  + digest + notification
```

- **Only speech is stored.** Silence is dropped before anything is written. Every utterance is kept as lossless FLAC with its transcript next to it.
- **Transcription is local and free.** It uses `whisper-large-v3-turbo` via MLX on Apple Silicon and transcribes a 12 s clip in about 1.4 s on the M1 Pro.
- **Triage runs once per thought.** A thought is a run of speech that ends after 90 s of quiet. A headless Codex run (`codex exec`, GPT-6-Astra on high reasoning, full permissions, cwd `/path/to/projects`) classifies it as `ambient`, `personal`, `log` or `actionable`. For actionable items it writes a self-contained brief.
- **Dispatches are Ribbon Field threads.** They are YOLO agent-coord threads on Codex, created by a session named **"Ramble voice dispatcher"**. They appear in the Ribbon Field app like any other thread.
- **Everything lands in a daily digest** at `~/.ramble/digest/YYYY-MM-DD.md`. You get a macOS notification when something is dispatched.

## Use it

The menu bar icon is the switch: 🎙 means listening, 🎙̸ means off, ⏸ means paused. The menu has Turn on/off, Pause 1 h or 3 h, today's digest, the data folder and the log. macOS also shows its orange mic dot while ramble is listening.

```bash
ramble on                # start listening
ramble off               # stop
ramble off 2h            # pause; resumes by itself
ramble status            # state, today's counts, last thing heard
ramble tail              # live transcripts as they arrive
ramble today             # today's digest (what was decided and dispatched)
ramble thoughts          # recent thoughts with classification
ramble show <id>         # a thought's transcript, triage decision and dispatches
ramble retriage <id>     # run triage again (e.g. after an error or a prompt change)
```

**First run:** the first time you turn it on, macOS asks "Ramble would like to access the microphone". Click Allow. If you click Don't Allow, re-enable it in System Settings → Privacy & Security → Microphone → Ramble.

Turn it off for guests, calls and movies. The triage prompt also treats TV, other people and phone calls as `ambient` or `personal` and never dispatches from them.

## Install / run

```bash
./bin/install      # uv sync, build ~/Applications/Ramble.app, start at login (launchd: local.ramble), symlink ~/.local/bin/ramble
./bin/uninstall    # stop and remove the app and login item (keeps ~/.ramble)
launchctl kickstart -k gui/$(id -u)/local.ramble   # restart after code changes
ramble run         # foreground, no menu bar (debugging). Don't run it alongside the app.
```

`Ramble.app` is a small Swift launcher (`macos/launcher.swift`) that runs `.venv/bin/python -m ramble.app` as a child. It exists so macOS gives mic permission to "Ramble" rather than to whatever terminal started it. Child processes inherit the app's permission.

## Testing without talking

```bash
ramble say "I want a prototype that turns my voice notes into GitHub issues"   # macOS TTS -> full audio pipeline
ramble ingest ~/some-recording.m4a                                              # any audio file through VAD + Whisper
ramble inject "text of a thought"                                               # skip audio; triage + dispatch only
```

Use `RAMBLE_HOME=/tmp/ramble-test` to test against a throwaway data dir. You need a `ramble run` with the same `RAMBLE_HOME` to process the queue. Set `"dispatch_daily_cap": 0` in its config to test triage without spawning agents.

## Data (`~/.ramble`, never in the repo)

| Path | What |
|---|---|
| `audio/YYYY-MM-DD/HHMMSS-xxxxxx.flac` (+ `.txt`) | Raw speech segments and their transcripts |
| `ramble.db` | SQLite: `segments` → `thoughts` → `dispatches` (schema in `ramble/db.py`) |
| `digest/YYYY-MM-DD.md` | Human-readable daily log of decisions and dispatches |
| `triage/<thought-id>.json` | Exact triage input, Claude's raw output, stderr |
| `logs/ramble.log` | App log; `logs/launchd.log` holds crashes before logging starts |
| `state.json` | On/off/paused switch (written by the menu and the CLI, polled every second) |
| `config.json` | Overrides for the defaults in `ramble/config.py` |
| `agent_coord_session` | Session id of "Ramble voice dispatcher" in agent-coord |

## Settings

Put overrides in `~/.ramble/config.json` and restart the app. The defaults are in `ramble/config.py`.

| Key | Default | Notes |
|---|---|---|
| `input_device` | system default | sounddevice name or index, e.g. a USB conference mic |
| `vad_threshold` | 0.5 | Raise it if background noise gets captured |
| `segment_silence_s` | 0.9 | Pause that ends one utterance segment |
| `thought_gap_s` | 90 | Quiet time that closes a thought and sends it to triage |
| `thought_max_s` | 900 | Force-close a very long ramble |
| `whisper_model` | `mlx-community/whisper-large-v3-turbo` | Any mlx-whisper model |
| `language` | auto | Set `"en"` to force English |
| `triage_backend` | `codex` | `codex` (`codex exec`) or `claude` (`claude -p`) |
| `triage_model` | `gpt-6-astra` | Model for the chosen backend (e.g. `"sonnet"` with `claude`; `null` = CLI default) |
| `triage_effort` | `high` | Codex reasoning effort |
| `dispatch_client` | `codex` | agent-coord client for dispatched threads |
| `dispatch_daily_cap` | 6 | Max threads spawned per day; above it, items are logged |
| `notify` / `ntfy_topic` | true / null | macOS notifications; set a topic for ntfy.sh phone pushes |

**Cost and speed:** with Codex and GPT-6-Astra on high, a triage run took 8–15 s and about 28k tokens in testing. The earlier Claude backend (Fable 5.1) cost about $0.61 per run. Transcription costs nothing.

## Code map

- `ramble/recorder.py`: mic stream, VAD segmenter, FLAC writer, and `ingest_file` (the same path, used for files and tests).
- `ramble/vad.py`: Silero VAD v5 on onnxruntime, no torch. The model is in `models/silero_vad.onnx` (MIT).
- `ramble/transcribe.py`: MLX Whisper worker, a hallucination filter, and grouping segments into thoughts.
- `ramble/router.py`: closes thoughts, runs triage (`codex exec --output-schema`, or `claude -p --json-schema`), dispatches via `agent-coord thread create`, writes the digest and sends notifications.
- `ramble/daemon.py`: wires the threads together; a mic-control thread follows `state.json`.
- `ramble/app.py`: rumps menu bar UI. `ramble/cli.py`: the `ramble` command.
- `prompts/triage.md`: the triage agent's instructions (routing rules, project map, dispatch brief format). Edit this to change behavior.

## Known limits / next ideas

- It only uses the laptop mic. A USB conference puck in a corner (set `input_device`) or other devices posting audio to an ingest endpoint are the obvious next sources. `ingest_file` is already the seam for that.
- There's no speaker identification. TV and other voices are filtered by the triage agent's judgment, not by voice ID.
- Triage can't add to an existing dispatched thread yet; it logs continuations instead. `agent-coord send` would be the way to add that.
- If you click Allow on the permission prompt while ramble is set to off, it opens the mic and closes it again right away. That's expected.
