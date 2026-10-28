# Repository boundary and privacy

Ramble has its own Git repository. Runtime settings and personal data stay outside the checkout.

Keep the installed checkout in place when migrating. A separate
GitHub repository does not require relocating it. This preserves the installed
app, CLI links, existing agent working directories, and Companion's sibling
imports and report publication. Repository separation does not remove the
runtime integrations below.

## What belongs in Git

Application source, prompts, tests, installation scripts, documentation,
`pyproject.toml`, `uv.lock`, and `models/silero_vad.onnx` belong in the repository.
The Python environment and downloaded Whisper models do not.

Recordings, transcripts, digests, triage audits, usage records, work-session
history, global instructions, logs and settings stay in `~/.ramble` (or a
throwaway `RAMBLE_HOME`). Companion's content and credentials stay in
`~/.ramble-ipad-display`. External API credentials stay in their existing secret
files or environment variables. `.gitignore` also guards against common copies
of these files appearing inside the checkout; it cannot untrack a file that
was already committed.

## Privacy and publication

Personal context belongs in the local Global instructions editor, not in source
files or examples. Location, project paths and recognition vocabulary can be
configured locally. Never commit recordings, transcripts, databases, credentials,
or machine-specific network names. Use synthetic dialogue in prompts, documentation
and test fixtures; replacing a name alone does not remove details about routines,
relationships, events, or plans. Describe diagnostic failures by their technical
conditions and results, without quoting private speech or copying real request IDs,
device inventories, or personal schedules. Keep original evidence in the private
runtime home or a throwaway test home outside Git.

Existing clones and backups can contain old history after a rewrite; publishing a repository needs a separate privacy,
licensing and third-party model notice review.

## Dependencies and location assumptions

| Component | Boundary |
|---|---|
| Python environment | Python 3.12 or later; package dependencies and resolved versions are in `pyproject.toml` and `uv.lock`. Install with `uv sync --locked`. |
| Audio/transcription | Apple Silicon/macOS, MLX Whisper, NumPy, SciPy, ONNX Runtime, sounddevice/PortAudio, soundfile, rumps/PyObjC. Whisper model weights use their external download cache; the Silero VAD model is committed. |
| Native app | Swift compiler, macOS signing tools and launchd. `bin/install` records the checkout path in `Ramble.app`; the app owns microphone permission. |
| Ribbon Field / Agent Coord | Installed `agent-coord` CLI and a compatible running Ribbon Field app for persistent work sessions, dispatch, steering and interruption. No source import or Python package dependency on its repository. |
| Model providers | Authenticated Codex or Claude CLI for model calls. Jev uses a credential outside Git and falls back to full triage if unavailable. |
| Companion | Separate sibling `ramble-ipad-display` checkout and running service. Ramble resolves its publisher relative to `config.REPO`; Companion imports Ramble from its sibling and uses Ramble's CLI. Prompts also name absolute helper paths. |
| Speech | Local Kokoro endpoint by default, optional ElevenLabs credential, and macOS speech fallback. |
| Personal actions | Stoic service for reminders/Toggl actions; credentials remain outside the repository. |

There are no local path dependencies in the Python dependency manifest or lock
file that import Playground or Ribbon Field. These runtime contracts still
matter to another developer cloning the project. This is an independent source
repository, not a self-contained distribution.

If the checkout is relocated later, update the Companion lookup/import paths,
prompt helper paths and app installation together; test automatic publication
and reconnect recovery before removing the old path. Keep `~/.ramble` intact,
and never change `state.json` to perform a migration.
