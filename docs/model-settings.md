# Model and reasoning settings

Use **Ramble → Models and reasoning** in the Mac menu, or **Settings** in
Companion at `/ipad/settings`. Choose a model and one of its supported reasoning
levels separately for quick voice replies, background thought review and new
work sessions. Mac choices save immediately; Companion has a **Save settings**
button. **Reload saved** refreshes settings changed on another device and asks
before discarding unsaved choices.

Models and supported reasoning levels come from the running Ribbon Field
provider through `agent-coord thread models`; no model turn or agent is started.
If discovery fails, the UI shows an error and a retry rather than inventing a
model list. Existing custom selections remain visible until changed. Thought
review controls are disabled when `triage_backend` is Claude; its existing
settings can still be edited in config.json on the Mac.

Service discovery resolves the installed `~/.local/bin/agent-coord` launcher
and reuses Ramble's existing `agent_coord_session` registration when there is
no interactive caller. It does not register a triage thread or start an agent.

The shared source is `~/.ramble/config.json` (or `RAMBLE_HOME/config.json`).
Only model/effort keys are changed, preserving unrelated settings. Writes use an
atomic replacement, a process-shared lock and a version check. A stale editor
cannot overwrite newer changes. Nothing writes the microphone switch.

Quick replies and Codex thought reviews read the selected pair at the beginning
of every invocation. New work session creation reads its selected pair before
submission. No restart is needed after saving. Requests already running and
existing work sessions keep their model; their settings are not changed by this
defaults editor. Work sessions can inherit provider defaults by selecting
**Provider default**.

Say **“Ramble, start a new session with GPT Astra ultra”** or **“…Astra max”**
to choose a model and reasoning level for that session. Other model names and
levels are resolved against the live provider catalog. An explicit new-session
request starts a separate foreground conversation; it can ask what to work on
if you have not supplied a task. The chosen pair is saved with its queued launch
and survives restart. It does not change the quick responder, saved defaults or
existing sessions. A model without an effort uses its provider default; an effort
without a model uses the saved/default work model. Unsupported or ambiguous
choices need clarification instead of silently falling back.

New sessions are explicitly told they are in voice mode, to acknowledge in
`[say]` tags, and to give concise spoken progress every 30–60 seconds while work
continues and public commentary is available between tool calls. Private reasoning
is never spoken. Persistent work sessions have no 120-second Ramble turn deadline;
the CLI's short wait only submits or observes their durable commands.

The quick-voice hard deadline follows the chosen reasoning level when
`fast_timeout_s` is null (the default): none/minimal/low get 30 seconds, medium
60, high 90, xhigh/max or an unspecified level 120, and ultra 900 (15 minutes).
This is a bounded
latency allowance, not a guarantee of completion. Saving a higher reasoning level
does not leave it on the low-effort deadline. The existing wait notice still
starts after 12 seconds. A positive numeric `fast_timeout_s` overrides this policy
after restarting the app; no model downgrade or automatic retry occurs.

Companion requires a paired reader and JSON with `X-Ramble-Edit: 1` to save.
The publishing token cannot control models. `GET /ipad/api/model-settings`
returns settings, version and the discovered catalog; `POST` accepts
`{settings: {fast: {model, effort}, triage: {model, effort}, work: {model, effort}}, version}`.
Unpaired requests return 401, missing edit headers 403, unsupported selections
400, stale versions 409 and storage/provider failures 503. No credentials are
sent to the browser.
