# ramble

Ambient voice capture for your laptop. Turn it on, think out loud, and an agent works out what to do with what you said.

See the
[repository boundary and privacy review](docs/repository-boundary.md) for what
is stored in Git, the runtime dependencies, and why the installed checkout path
stays unchanged.

```
mic ─► speech detection ─► FLAC segments ─► local Whisper ─► thoughts ─► Jev filter
       (Silero VAD)        (~/.ramble/audio)   (MLX, ~1s)     (grouped)      ├─ confident background/casual → digest
                                                                         └─ useful/uncertain → triage agent → threads + digest
```

- **Only speech is stored.** Silence is dropped before anything is written. Every utterance is kept as lossless FLAC with its transcript next to it.
- **Transcription is local and free.** It uses `whisper-large-v3-turbo` via MLX on Apple Silicon and transcribes a 12 s clip in about 1.4 s on the M1 Pro.
- **Recognition hints:** English and a short list of names/project terms are supplied to Whisper. When hinted text contains an address, a second pass without hints must also recognize the name or one of its known mishearings; otherwise the unhinted transcript is kept. This prevents hints from turning ordinary speech such as "Right, but..." into a wake name. That confirmation adds one local decode for addressed clips.
- **Triage runs once per thought.** A thought is a run of speech that ends after 90 s of quiet. Jev first checks the entire unclaimed thought. Confidently background or casual speech gets a simple digest entry without running Codex; useful notes, possible tasks and uncertainty go to headless Codex (`codex exec`, GPT-6-Astra on high reasoning, full permissions, cwd from `triage_cwd`) for a summary, classification and any self-contained work brief.
- **Dispatches are Ribbon Field threads.** They are YOLO agent-coord threads on Codex, created by a session named **"Ramble voice dispatcher"**. They appear in the Ribbon Field app like any other thread.
- **Everything lands in a daily digest** at `~/.ramble/digest/YYYY-MM-DD.md`. You get a macOS notification when something is dispatched.

## Use it

The menu bar icon is the switch: 🎙 means listening, 🎙̸ means off, ⏸ means paused. The menu has Turn on/off, Pause 1 h or 3 h, today's digest, the data folder and the log. macOS also shows its orange mic dot while ramble is listening.

**Mini window:** a small floating pill stays visible above other apps, across
Spaces and alongside full-screen windows. It starts centered above the Dock,
at 48 × 22 points while idle. Drag it anywhere; its display and position are
remembered. **Show mini window** in the menu toggles visibility, and **Reset
mini window position** returns it to the default. Right-click the pill for the
same position reset. It does not take keyboard focus or change microphone state.

Hover over the pill to reveal two controls: **sparkles** opens the existing
model/reasoning menu, and the **expand arrows** open Ramble's native app window. Choose
models for quick replies, thought review or new work sessions from the popup.
The current activity stays visible between the buttons, including live
waveforms. The pill stays expanded while the model menu is open and contracts
when the pointer leaves. Drag the center to move it.

While active it expands to 174 × 38 points:

| State | Mini window |
|---|---|
| Capturing speech | Mint waveform from measured microphone levels |
| Wake name recognized | Violet “I'm here” pulse, or a violet listening waveform while you continue |
| Transcribing / background triage | Blue “Processing” dots |
| Preparing a reply | Amber “Thinking” dots |
| Foreground work session running | Blue “Working” pulse and dots, including quiet work |
| Speaking | Violet waveform from the actual output callback; silence stays flat |
| Microphone off / timed pause | Small gray / amber indicator |
| Microphone or response error | Error state, with details on hover |

Voice muting leaves visual thinking and work activity available. Output levels
expire after 350 ms without fresh callbacks, so a crashed output helper cannot
leave a speaking waveform stuck. Reduce Motion disables decorative pulsing and
size animations; the waveform still shows measured levels. Window preferences
live in `~/.ramble/mini-window.json`, independently of the microphone switch.

**Ramble window:** choose **Open Ramble…** in the menu or expand the floating
pill. The resizable window has **Settings** and **Token usage** pages. Settings
includes model/reasoning controls, microphone and voice switches, mini-window
visibility, and the same global instructions editor used by Companion. Closing
the window returns to the mini experience; Ramble keeps running, and reopening
reuses the window and any unsaved instructions draft.

**Token usage:** select Today, 7 days, or All recorded to see input, cached input,
output, and total tokens, attributed by activity/provider/model or individual
request. Local counters appear first; linked work-session usage refreshes in
the background through Agent Coord's supported `thread usage` command. Linked
conversations show **lifetime** totals separately because they can include work
started outside Ramble. Snapshots replace prior snapshots; they are never added
to the dated per-call totals. Refreshing usage never starts an agent.

The private `~/.ramble/usage.sqlite3` ledger keeps counter metadata, call/request
IDs, models, outcomes and source coverage, without prompts or audio. Each retry
has its own call ID; reimporting the same call does not count it twice. Retained
fast-call traces and triage audits backfill older usage on first opening the
page. This history is partial: traces rotate, old triage retries were overwritten,
and historical triage dates use audit-file modification time. Recovery examines
at most 2,000 files / 64 MiB per refresh; skipped records are reported.

Missing counters appear as **—**, and **≥** marks a measured subtotal with missing
calls. Cached input is already included in input; reasoning tokens are already
included in output. Claude cache creation/read counts are incorporated into its
input total when the provider reports all splits. Jev calls without counters
remain attributed with unknown token usage. Local Whisper and speech-audio
billing are outside this token view; these counters are not an invoice. Linked
sessions are deduplicated and limited to the 100 most recent recorded links;
unavailable refreshes retain the previous snapshot with a stale indicator.

```bash
ramble on                # start listening
ramble off               # stop
ramble off 2h            # pause; resumes by itself
ramble status            # state, today's counts, last thing heard
ramble voice off         # mute all Ramble audio; keep transcription and work running
ramble voice on          # enable voice responses again
ramble voice status      # show the independent voice switch
ramble tail              # live transcripts as they arrive
ramble today             # today's digest (what was decided and dispatched)
ramble thoughts          # recent thoughts with classification
ramble show <id>         # a thought's transcript, triage decision and dispatches
ramble retriage <id>     # run triage again (e.g. after an error or a prompt change)
ramble say "Got it."  # speak aloud; never ingest the reply as a thought
```

**Mute voice responses while keeping transcription:** say **“Disable voice
responses”** (or **“Ramble, disable voice responses”**), choose **Disable voice
responses** in the Mac menu, or run `ramble voice off`. Ramble silences spoken
replies, questions, work updates, acknowledgement chimes and working tones.
The voice command is applied locally after transcription without a chime or
spoken confirmation, even if the responder is busy or unavailable. Active
playback is interrupted; queued audio is cancelled. Transcription, task routing,
text results and notifications continue.

Say **“Enable voice responses”**, use the menu, or run `ramble voice on` to
resume audio. Old replies are discarded rather than played after unmuting.
The switch persists across restarts in `~/.ramble/voice.json`, independently of
the microphone switch and timed pause. `ramble status` and the menu show it.
Voice commands require the microphone to be listening and cannot interrupt
speech that the playback echo guard drops; use the menu or CLI during playback.

**Quiet snooze for sleep:** choose **Pause 1 hour** or **Pause 3 hours** from the
menu, or run `ramble off 2h` with your chosen duration (`90m`, `4h`, etc.).
`ramble status` shows the resume time and the menu icon becomes ⏸. Timed pause
stops capture, cancels queued fast replies and chimes, interrupts active Ramble
playback, holds ambient triage, and suppresses new dispatches, spoken questions,
macOS notifications and ntfy pushes. `ramble say` also refuses playback during
a timed pause; `say --no-play` still generates a file silently.

Listening resumes automatically at expiry while the app is running, without a
chime or announcement. Choose **Turn on** or run `ramble on` to resume early.
Unprocessed thoughts from before the pause can be triaged after resuming; stale
fast replies stay cancelled. Work already handed to other agents continues,
and notifications from other apps are controlled separately by macOS Focus.
`ramble off` without a duration remains the microphone switch; use a timed
pause for the quiet behavior described here. Pause/resume must be selected in
the menu or terminal, not by voice while the mic is stopped.

**First run:** the first time you turn it on, macOS asks "Ramble would like to access the microphone". Click Allow. If you click Don't Allow, re-enable it in System Settings → Privacy & Security → Microphone → Ramble.

Turn it off for guests, calls and movies. The triage prompt also treats TV, other people and phone calls as `ambient` or `personal` and never dispatches from them.

## Talking to Ramble (fast lane)

Say its name to address Ramble: "Ramble, what's the weather?", "hey Ramble, what are my agents doing?", or "...right, Ramble?" at the end of a sentence. With a known foreground conversation, ordinary requests go directly to its owner without a routing-model call or queued acknowledgement. Otherwise the fast responder answers or chooses an owner. A bare "Ramble?" waits up to 8 s for the request. For 20 s after a direct reply, you can follow up without the name. Background work updates do not open that window.

A quick exchange can grow into a persistent conversation: **“Ramble, let's keep
talking that through”** or **“Let's work on that together.”** The responder chooses
the initial route from your intent. Ongoing discussion and
brainstorming count, even without tools or a deliverable; brief banter and
self-contained questions can stay quick replies. A related existing session keeps
the conversation. After the automatic follow-up window, Jev can recognize a clear
continuation of a recent quick exchange without hearing the name again.

**Picking up a quick conversation after a pause:** for up to ten minutes after a
quick reply, Jev checks whether new speech is both addressed to Ramble and related
to that exchange. Both scores must reach `chat_followup_threshold` (default 0.8).
For example, “Will you continue from the last response?” can resume the exchange;
“The stopwatch is on the desk,” quoted speech, another addressee and ambiguous
fragments stay passive unless both checks pass. The normal responder then decides
whether to answer briefly or continue into collaborative work with the same context.
The first 20 seconds still use the existing automatic follow-up path.

Only a successfully played quick reply opens this additional window. Queued,
failed and muted replies and work acknowledgements do not. The conversation and
pending checks survive restart; a new addressed request, Stop/quiet/focus change,
pause or expiry invalidates an older pending check. Nearby transcript fragments
are checked together. A rejection or API failure returns the speech to background
triage without a chime, reply or action. Clear accepted continuations use normal
routing, including its existing action receipts and work-session rules.

Set `chat_followup_s` to shorten the window (maximum 600 seconds), or zero to
disable it. Jev receives only the candidate speech and up to eight recent addressed
messages, each capped at 4,000 characters. Behavior is in
`prompts/chat-followup.json`. Authentication uses `JEV_API_KEY`, then
`TYPESAFE_API_KEY`, then `jev_api_key_file` (for example,
`~/.config/ramble/jev-key`); setting that file option to null allows
`JEV_API_KEY_FILE` or `TYPESAFE_API_KEY_FILE`. Keep the credential outside the repo
and config. The pinned model is `jev-1.13.0`, with a three-second timeout and a
30-second cooldown after service failure. There are no automatic API retries.
`chat_followup_decision` latency events record scores and segment IDs without
credentials or transcript text. This is text-based inference, not speaker
identification; conservative thresholds can still miss a real continuation.

When creating or continuing a work session, Ramble includes the same recent
addressed exchanges used to choose the route: up to eight messages from the past
ten minutes, capped at 4,000 characters per message. Your exact new request stays
separate from this reference context. Passive captured speech is not copied into
the work session, and earlier actions are not requests to execute them again.
The context is saved with the queued work command, so a restart after queuing
does not lose it. Jev's additional quick-chat window is described above.

**Direct conversation delivery:** ownership lasts for one hour of inactivity by
default (`work_followup_s`; zero disables it). Accepted user messages and live
final answers renew it. Successful result playback extends it from when the
answer finished speaking; background progress, polling and recovered history do
not renew it. The owner and queued inputs survive restart. After expiry the fast
responder can still select that same session; expiry does not close it.

An unnamed follow-up can also reach that owner within the same window. This requires
a connected foreground session that is running or idle and has not been quieted.
A separate read-only classifier must first recognize a conversational question,
correction, answer or instruction directed to the assistant. Recent timing selects
the existing owner; topic changes stay in that conversation, including moving
[historical text removed for privacy]
time since the last live exchange and the bounded follow-up window. Topic overlap
alone does not establish an address; ambient commentary, quoted speech and
ambiguous fragments are rejected. The
window uses conversation events and successful result delivery.
Explicit requests for separate sessions or agents, reminders, time-entry changes
and report-publication requests still use the fast responder and their existing
adapters. Routing exceptions look for explicit requests; merely mentioning agents
or sessions, checking earlier instructions, and discussing work already underway
stay with the current owner. Complete addressed controls such as “Ramble, stop work”
and “Ramble, stop talking” run locally, even while the model is busy or unavailable.
After the window expires, say “Ramble” again. This is text-based address checking,
not speaker identification, and it can miss a follow-up or misclassify speech.
`work_followup_decision` latency events record the admission decision and elapsed
conversation time without transcript text. `direct_work_input` records direct
delivery to the durable command queue. Captured inputs have a separate two-minute
processing deadline; Stop, quieting or switching foreground invalidates the old
owner instead of silently redirecting queued speech.

Candidates remain reserved while the address check runs and survive restart.
Only a positive result forwards the exact speech to its captured owner; Stop,
quieting, a foreground change or expiry prevents a delayed admission. No new agent
or immediate-action adapter runs through this path. Rejected candidates return
to ordinary background triage. Accepted speech has separate thoughts, so it
cannot reset the 90-second quiet period of an unrelated background thought.

Whisper variants including "Grable" and "Granville" also trigger replies. After an addressed segment, a continuation starting within 8 s is included even before Ramble has spoken; each accepted segment extends that window.

**Audio feedback:** a quiet 140 ms rising chime means Ramble heard your address and is listening. It can arrive while you are still speaking; it does not confirm an action or completed work. A low 200 ms descending tone means answer generation or spoken playback failed. Continuations do not ding repeatedly; cues are rate limited and skipped if stale or speech playback is busy. They keep the mic open and do not extend the conversational follow-up window. Set `"audio_cues_enabled": false` to disable them, or `"audio_cues_volume": 0.5` (0–1) to adjust loudness, then restart the app.

For an opening "Hey Ramble", capture offers up to three short prefixes, at about
1.2, 2.4 and 4 seconds, to the existing Whisper worker. A confident, unhinted
opening address can trigger the chime before the complete transcript arrives.
The full recording and normal name confirmation still determine whether to
accept a request; previews never route work, publish partial text, or open a
follow-up window. Complete segments take priority over queued previews, and
stale or cancelled previews are discarded. A preview can still mishear a name,
so this early cue means listening only. Short utterances, missed previews and
later addresses use the full-transcript acknowledgement. Set
`"early_wake_enabled": false` to use only that acknowledgement.

While preparing an addressed response, Ramble plays a soft 195 ms two-note pulse
every 2.5 seconds, starting after a one-second settling delay. It is about a
quarter of the ding's peak amplitude. The pulse yields while you or Ramble speak
and remains available through request collection, answer generation, speech
queueing and synthesis. It stops when the output callback accepts the first
spoken frames, the request
fails/cancels, or Ramble is paused. A bare wake name and passive background
classification do not start it. An early preview containing both the name and
request can arm it before full transcription; the pulse still yields while you speak.
Set `"audio_working_enabled": false` to keep the ding without the repeating pulse,
or adjust `"audio_working_interval_s": 2.5` (minimum 1 second), then restart.

The pulse also follows the foreground work-session agent while its request is
queued or running, including direct follow-ups. It yields during spoken updates
and resumes between them until the agent finishes. Quieting or stopping the
session, switching foreground, disconnecting, muting voice or pausing listening
suppresses its pulse. Background delegated sessions do not pulse.

Cues and spoken replies share a persistent output-only stream, avoiding a new
`afplay` process and device open for every reply. A small supervised helper keeps
output warm with silence and reports callback progress. It converts generated
speech to the output device's sample rate before playback; completion waits for
the final frame's estimated device playback time. First-frame timing is logged
as a device estimate, not an acoustic measurement.

The helper reopens output after a device change, crash or stall. Interrupted
speech is marked failed and never automatically replayed; the next reply can
use the recovered stream. Cues still expire after two seconds. Mute, timed pause
and app shutdown stop output; a lost parent also ends the helper. Disabling
chimes alone keeps output warm for spoken replies. Setting both `fast_speak` and
`speak_questions` false disables background warming. See
[persistent speech output](docs/speech-output.md) for validation and diagnostics.

The fast lane waits for current speech and pending transcription before answering.
Its `fast_coalesce_s` silence window (default 1.2 seconds) counts silence already
elapsed during transcription, adding only the remaining wait. New voice activity
extends the window even before its transcript arrives. If you continue a request
while an answer is being generated, it updates the answer with that continuation
instead of speaking the stale fragment response. Its prompt also includes the
last twelve captured segments from the past ten minutes, so a question like
"did you hear that?" can refer to speech that missed the name trigger. Spoken
replies still suppress mic capture during playback; the chime itself does not.

- `ramble/fastlane.py` detects addressing, using the name regex (Whisper spellings like "Rambo" and "rumble" count, "rambling" doesn't) or the follow-up window from `speech_playbacks`. It runs a quick `codex exec` (`fast_model` gpt-6.1-sol, low effort, cwd `~/.ramble`, prompt `prompts/fast.md`) and classifies the reply or work route. Speech delivery runs separately from input and execution.
- **Work together or delegate.** Collaborative research, writing and coding stay in a persistent work session. Independent background outcomes can go to a separate agent. Follow-ups keep their existing owner; an explicitly named existing agent is reused. Quick answers and structured reminder/time-entry actions remain immediate.
[historical text removed for privacy]
- **Delivery is recorded separately.** Fast replies are queued to a speech worker, with played/failed/cancelled receipts. Accepted addressed speech is durably claimed before classification, so slow triage cannot repeat it when speech or execution is delayed.
- **Failures are explicit.** The responder's automatic hard deadline follows the
  selected reasoning effort: 30 seconds for low/minimal/none, 60 for medium,
  90 for high, 120 for xhigh/max or an unspecified level, and 900 for ultra. The model and
  reasoning choice are preserved. `fast_timeout_s: null` selects this policy;
  a positive number sets an explicit deadline. A wait notice after 12 seconds
  remains independent of the hard deadline. Failure starts a 30-second cooldown
  (`fast_failure_cooldown_s`).
  A timeout does not rerun the request for every new utterance. Later requests
  during cooldown receive an immediate service-error reply. Failed inputs retain
  their text and error in `session_inputs`; they are not marked handled or
  automatically replayed into workers. Completion remains unconfirmed if a fast
  lookup may already have acted before its response failed.
  Codex HTTP 429 retry notices end the fast call immediately and extend the
  cooldown to the server's retry delay when supplied. The log records that delay
  without request text. Direct replies and work-follow-up classification share
  this cooldown; pending follow-up candidates remain reserved until it ends.
  Deadline and rate-limit cancellation stop the entire
  owned Codex process group, including an npm launcher's native child.
  Thought summaries follow durable input receipts: pending, response failed,
  queued for a work session, or response prepared. Preparing or playing a reply
  does not establish task completion. Outcomes update already-triaged thoughts;
  startup also repairs older premature success labels without retrying work.
- **Unnamed speech stays passive unless a conversational window admits it.** Relevant work follow-ups can receive the existing owner's spoken updates.
- **Measured (generated audio, no playback):** a weather answer came back 9.0 s after the utterance was transcribed, and a work handoff took 5.6 s. Add about 1 s for silence detection and Whisper, and about 1.2 s of coalescing.

## Install / run

```bash
./bin/install      # uv sync, build ~/Applications/Ramble.app, start at login (launchd: local.ramble), symlink ~/.local/bin/ramble
./bin/uninstall    # stop and remove the app and login item (keeps ~/.ramble)
launchctl kickstart -k gui/$(id -u)/local.ramble   # restart after code changes
ramble run         # foreground, no menu bar (debugging). Don't run it alongside the app.
```

`Ramble.app` is a small Swift launcher (`macos/launcher.swift`) that runs `.venv/bin/python -m ramble.app` as a child. It exists so macOS gives mic permission to "Ramble" rather than to whatever terminal started it. Child processes inherit the app's permission.

Only one capture pipeline can run per `RAMBLE_HOME`. The app and `ramble run`
hold an operating-system lock on `daemon.lock`; a second launch exits before
starting workers. The lock releases automatically when the process exits,
including after a crash. Leave the lock file in place. Separate test homes can
run independently. Fast replies also use a separate temporary directory for
each model invocation, so concurrent calls cannot overwrite or delete each
other's results. Logs include process IDs to help identify unexpected launches.

## Testing without talking

```bash
ramble say "I want a prototype that turns my voice notes into GitHub issues" --no-play --output /tmp/ramble-test/fixture.wav
ramble ingest /tmp/ramble-test/fixture.wav                                      # generated audio -> full audio pipeline
ramble ingest ~/some-recording.m4a                                              # any audio file through VAD + Whisper
ramble inject "text of a thought"                                               # skip audio; triage + dispatch only
```

Use `RAMBLE_HOME=/tmp/ramble-test` to test against a throwaway data dir. You need a `ramble run` with the same `RAMBLE_HOME` to process the queue. Set `"dispatch_daily_cap": 0` in its config to test triage without spawning agents.

## Reminders through Stoic

[historical text removed for privacy]
saves the reminder in Stoic immediately after the request finishes, then confirms
the saved date and time. It does not wait for thought triage or create a worker.
Stoic delivers its existing phone notifications independently of this Mac.

You can ask for your reminders, move one to a new time, mark an occurrence done,
or cancel a reminder (including its repeats). “Check in with me Friday at noon
about the report” creates an assistant-owned check-in. Daily, weekly and monthly
repeats are supported. Rescheduling moves **one occurrence**, not the entire
recurring schedule. Missing times, ambiguous targets and AM/PM are clarified.
Use Ramble's name or an active conversational follow-up; passive thoughts do not
silently schedule reminders.

The Companion's **Reminders** page lists the same Stoic records and supports
create, reschedule, done and cancel. Times use the configured timezone, including when
the iPad is in another timezone. A local time skipped or repeated by daylight
saving needs a different time or an explicit offset. The Stoic list API exposes
only the newest 200 jobs; Companion shows a notice if that bound is reached.

Authentication uses `STOIC_API_TOKEN`, then `STOIC_API_TOKEN_FILE`, then
`~/.config/stoic/codex-token`. Credentials remain on the Mac. The read-only helper
`<ramble-checkout>/bin/reminders list` supplies current IDs for the
fast responder. Mutations are structured output, executed by `ramble/reminders.py`
only after request coalescing; they must not be performed through model shell calls.
Fast replies are replaced with confirmed tool results, and action receipts are
recorded in thoughts before speech so playback failure cannot trigger a duplicate
worker. Triage logs reminder operations rather than dispatching them.

`~/.ramble/reminders.sqlite3` is a private operation receipt journal, not another
scheduler. Identical request IDs replay saved results, and matching active title,
time and recurrence prevent duplicate creates. GETs can retry once; writes never
retry automatically. A lost create response can be reconciled by its source marker
in the Stoic description. Unknown outcomes stay explicit until checked, rather
than reporting success or blindly repeating a write. This adapter does not claim
server-side exactly-once delivery, and does not speak reminders when they become due.

Tests use fake Stoic responses and throwaway homes, without phone notifications,
live writes, microphone capture, playback or worker dispatch.

## Toggl timers through Stoic

Say **“Ramble, start a new timer for debugging under engineering.”** Ramble reads
the running entry and all connected Toggl workspaces/projects, resolves the
requested project, and starts a new timer through Stoic. It asks when project or
workspace names are ambiguous. Starting replaces the running timer; the existing
entry is not renamed. Failed or unconfirmed starts are never retried automatically.

Say **“Ramble, rename my current time entry to debugging voice capture”** or
**“Ramble, put my current entry under engineering.”** You can change both in one
request: **“Ramble, make this debugging Ramble under engineering.”** Ramble reads
your active Toggl projects, chooses a clear match, and asks when the choice is
ambiguous. It confirms the saved description and project after Stoic accepts the
edit. Edits preserve the existing start time and other fields. No new project is
created. Ask **“Ramble, what am I tracking?”** to read the current entry.

The read-only `bin/time-entry current` helper supplies actual entry/project IDs;
`bin/time-entry workspaces` lists workspaces and available projects even without
a running timer.
Structured `time_entry` output executes through Stoic after speech coalescing,
using the same credentials and private receipt journal as reminders. Stoic checks
that the running entry ID still matches and the project belongs to its workspace.
Unconfirmed writes are not retried automatically, and the slow lane does not
dispatch workers to repeat them. This feature edits only the running entry's
description and project, and supports starting new timers now. Stopping timers,
backdating and changing completed entries remain unsupported.

Timer/reminder requests stay with the structured executor even if a work route
is selected incorrectly. Companion messages use the same internal queue, and
its workspace shows action receipts separately from conversation status.
Accepted requests retain their identity; unsupported operations fail explicitly,
and uncertain writes are never replayed automatically. See
[action routing and execution receipts](docs/action-routing.md) for recovery,
capability checks, full triage audits and isolated validation.

## Speech generation

**Latency tracing:** the rotating `~/.ramble/logs/ramble.log` includes
`ramble.latency` JSON events for segment save, transcription, early wake acknowledgement, name/request
acceptance, collection, answer generation (including reruns), reply readiness,
speech-lock wait, synthesis and the playback command. Events carry wall and
monotonic seconds, segment IDs, and a request ID from the first queued segment
through speech. `speech_file_ready` records the actual engine, path and duration.
Stage durations use the monotonic clock; events contain no utterance text.
`early_wake_acknowledged` records a cue queued from a preview, not audible onset;
`transcription_decode` distinguishes `wake_preview` from complete-segment passes.
`answer` includes prompt construction and model/tool work. `synthesis` includes
provider fallback, when needed. Playback-command events bracket `afplay` through
the pause-aware runner; they **do not measure audible onset**. The existing
`speech_playbacks.started_at` is a feedback guard beginning 100 ms before the
command. Segment times include padding and approximate callback wall time,
not precise acoustic boundaries. For true wake-to-speech latency, record one
interaction on an external phone/recorder, marking wake onset, request end and
first spoken reply in that single recording. Exclude the acknowledgement chime.

`ramble say "Got it."` plays a reply through the configured speech provider. The default is the local Kokoro server at `http://127.0.0.1:8880/v1/audio/speech` using model `kokoro` and voice `af_heart`. Choose another installed Kokoro voice with `--voice af_bella` or `tts_voice` in config. `--output /tmp/reply.wav` chooses the saved WAV path; `--no-play` only generates audio. Generated replies otherwise live in `~/.ramble/speech/` and are retained until removed manually.

For ElevenLabs, merge these overrides into `~/.ramble/config.json` and restart the app:

```json
{
  "tts_provider": "elevenlabs",
  "elevenlabs_api_key_file": "~/.config/ramble/elevenlabs-key",
  "elevenlabs_voice_id": "CwhRBWXzGAHq8TQ4Fs17",
  "elevenlabs_model": "eleven_flash_v2_5",
  "elevenlabs_stability": 0.75,
  "elevenlabs_similarity_boost": 0.75,
  "elevenlabs_style": 0.0,
  "tts_speed": 0.95
}
```

Roger (the voice ID above) is a starting point for a restrained, TARS-like delivery: a mature American conversational voice described by ElevenLabs as laid-back and resonant. This is a recommendation based on the available voice descriptions, not a verified likeness or a clone. Eric (`cjVigY5qzO86Huf0OWal`) is a smoother alternative; Mike (`aWSwMfJOK8iYZivczje6`) in the Voice Library emphasizes deadpan humor. Use `--voice <voice-id> --no-play --output /tmp/ramble-test/sample.wav` to generate a comparison without speaker playback.

The API key is read from that file for each request, never copied into config. Keep it owner-readable (`chmod 600`); `ELEVENLABS_API_KEY` is an alternative when no key file is configured. ElevenLabs receives the reply text for speech synthesis. Audio uses 24 kHz PCM wrapped into a WAV, and `--voice` accepts an ElevenLabs voice ID. Set `tts_provider` back to `kokoro` to use the local server.

If the provider is unreachable, times out, rejects the credentials, or returns invalid audio, Ramble logs a warning and generates a WAV with macOS `say` instead. The fallback uses the system voice, or `tts_fallback_voice` (e.g. `Samantha`). Playback uses `afplay`; synthesis or playback errors exit visibly with a failure. No automatic dispatch acknowledgements are enabled.

Python callers use `from ramble.speech import speak; speak("Got it.")`. It blocks until playback ends and returns the audio path, engine, and duration. `synthesize(...)` generates without playback, and `play_audio(path)` applies the feedback guard to an existing audio file. Use the same `RAMBLE_HOME` as the listening app so they share the guard.

Before playback, Ramble commits a bounded window to `speech_playbacks` in its SQLite database. The mic worker drops chunks overlapping that window, resets VAD/padding, and also rejects overlapping complete segments before writing audio. Windows remain available for delayed recorder queues; a one-second tail covers room echo. This leaves `state.json` and the mic permission alone, and recovers automatically after a crashed player. Logs contain `feedback guard` drop/resume messages. Speech spoken by the user during a playback window is also dropped; unrelated players and explicit `ingest` imports are not covered. A longer echo tail can be configured for reverberant rooms.

**Delivery-app callouts:** an optional authenticated webhook lets iPhone/iPad Shortcuts app-open automations trigger a teasing or firm Uber Eats/DoorDash callout on the Mac, with a shared cooldown. See [setup, configuration and verification](docs/delivery-callouts.md); each device needs its own automations.

## Background thought filtering

Speech without a wake name still becomes a complete thought after the normal
quiet period. Jev receives the full unclaimed transcript, current triage rules,
saved preferences and the same recent-thought context as Codex. It never filters
individual segments before they have been grouped, so a useful idea at the end
of a long casual passage remains available for interpretation.

The first pass skips full triage only when Jev selects `ambient` or `personal`
with probability at least **0.95**, category confidence at least **0.8**, and a
separate "needs review" score at most **0.1**. These are model scores, not a
measured accuracy guarantee. Everything else uses the configured triage backend,
including useful reflections and notes that need a proper summary. A thought
within two hours of a recorded clarification question goes straight to full
triage, preserving short answers such as "yes".

Skipped thoughts retain their original audio and transcript and get a generic
"Background speech." or "Casual conversation." digest entry. Jev does not create
work briefs, dispatch agents or speak. Missing credentials, service errors,
timeouts, invalid responses and inputs too large for the filter all fall back to
full triage without truncating the thought. Set `triage_jev_enabled` to `false`
to disable this first pass. It uses the same Jev credentials, pinned model and
timeout as late quick-chat recognition, with a separate request slot so background
classification cannot occupy the chat classifier's slot.

`triage/<id>.jev.json` records the context, questions, scores and routing reason.
The usual `triage/<id>.json` records the final classifier output: Jev for skipped
thoughts, or the full triage backend for everything else. `ramble show <id>`
continues to show the saved transcript and decision.

## Data (`~/.ramble`, never in the repo)

| Path | What |
|---|---|
| `audio/YYYY-MM-DD/HHMMSS-xxxxxx.flac` (+ `.txt`) | Raw speech segments and their transcripts |
| `ramble.db` | SQLite: `segments` → `thoughts` → `dispatches` (schema in `ramble/db.py`) |
| `digest/YYYY-MM-DD.md` | Human-readable daily log of decisions and dispatches |
| `triage/<thought-id>.json` | Triage input and output from the final classifier (Jev, Codex or Claude) |
| `triage/<thought-id>.jev.json` | Jev first-pass context, scores, thresholds and fallback reason |
| `logs/ramble.log` | App log; `logs/launchd.log` holds crashes before logging starts |
| `state.json` | On/off/paused switch (written by the menu and the CLI, polled every second) |
| `mini-window.json` | Mini window visibility, display and normalized position |
| `usage.sqlite3` | Private token ledger, retained-history import stamps and linked-session snapshots |
| `output-meter.json` | Ephemeral output-callback level and freshness timestamp; no audio or text |
| `config.json` | Overrides for the defaults in `ramble/config.py` |
| `agent_coord_session` | Session id of "Ramble voice dispatcher" in agent-coord |
| `speech/*.wav`, `speech.lock` | Generated replies and cross-process speech serialization |

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
| `language` | `en` | English by default; set `null` for auto-detection |
| `whisper_initial_prompt` | `Ramble, Ribbon Field, Stoic, ElevenLabs.` | Vocabulary hints for names and project terms; set `null` or `""` to disable |
| `triage_backend` | `codex` | `codex` (`codex exec`) or `claude` (`claude -p`) |
| `triage_model` | `gpt-6-astra` | Model for the chosen backend (e.g. `"sonnet"` with `claude`; `null` = CLI default) |
| `triage_effort` | `high` | Codex reasoning effort |
| `triage_jev_enabled` | true | Jev first pass for complete background thoughts; uncertain/useful content uses full triage |
| `triage_jev_min_probability` | 0.95 | Minimum probability for the selected ambient/personal category |
| `triage_jev_min_confidence` | 0.8 | Minimum category confidence to skip full triage |
| `triage_jev_max_review` | 0.1 | Maximum "needs review" score to skip full triage |
| `dispatch_client` | `codex` | agent-coord client for dispatched threads |
| `dispatch_daily_cap` | null | Unlimited by default; positive integers cap daily threads; 0 disables dispatch |
| `notify` / `ntfy_topic` | true / null | macOS notifications; set a topic for ntfy.sh phone pushes |
| `tts_provider` | `kokoro` | `kokoro` or `elevenlabs`; both use the same feedback guard and macOS fallback |
| `tts_url` / `tts_model` | local port 8880 `/v1/audio/speech` / `kokoro` | OpenAI-compatible speech endpoint |
| `tts_voice` / `tts_speed` | `af_heart` / 1.0 | Installed Kokoro voice and speaking speed |
| `tts_timeout_s` | 30 | Synthesis timeout before macOS fallback |
| `tts_fallback_voice` | system default | Voice for macOS `say` (`say -v '?'` lists voices) |
| `tts_guard_tail_s` | 1.0 | Extra mic suppression after playback for room echo |
| `elevenlabs_api_key_file` | null | External secret file; otherwise reads `ELEVENLABS_API_KEY` |
| `elevenlabs_model` / `elevenlabs_voice_id` | `eleven_flash_v2_5` / Roger | Model and voice ID, independent of the Kokoro settings |
| `elevenlabs_stability` / `elevenlabs_similarity_boost` | 0.75 / 0.75 | Consistency and voice similarity settings |
| `elevenlabs_style` / `elevenlabs_use_speaker_boost` | 0.0 / true | Expressiveness and speaker boost; `tts_speed` applies to either provider |

**Cost and speed:** with Codex and GPT-6-Astra on high, a triage run took 8–15 s and about 28k tokens in testing. The earlier Claude backend (Fable 5.1) cost about $0.61 per run. Transcription costs nothing.

## Code map

- `ramble/recorder.py`: mic stream, VAD segmenter, FLAC writer, and `ingest_file` (the same path, used for files and tests).
- `ramble/speech.py`: ElevenLabs/Kokoro/macOS speech generation, guarded playback, and callable `speak` API.
- `ramble/vad.py`: Silero VAD v5 on onnxruntime, no torch. The model is in `models/silero_vad.onnx` (MIT).
- `ramble/transcribe.py`: MLX Whisper worker, a hallucination filter, and grouping segments into thoughts.
- `ramble/router.py`: closes thoughts, runs triage (`codex exec --output-schema`, or `claude -p --json-schema`), dispatches via `agent-coord thread create`, writes the digest and sends notifications.
- `ramble/triage_filter.py`, `prompts/triage-filter.json`: conservative Jev first pass for background thoughts, with full-triage fallback and decision audit records. `ramble/jev.py` provides the shared bounded transport.
- `ramble/daemon.py`: wires the threads together; a mic-control thread follows `state.json`.
- `ramble/app.py`: rumps menu bar UI. `ramble/cli.py`: the `ramble` command.
- `ramble/app_window.py`: reusable native Settings / Token usage window. `ramble/usage.py`: counted-once provider usage, recovery and Agent Coord lifetime snapshots.
- `ramble/mini_window.py`, `ramble/activity.py`: native floating panel, read-only activity state and expiring output levels. UI animation never opens an audio device or queries SQLite on the main thread.
- `prompts/triage.md`: the triage agent's instructions (routing rules, project map, dispatch brief format). Edit this to change behavior.

## Known limits / next ideas

- It only uses the laptop mic. A USB conference puck in a corner (set `input_device`) or other devices posting audio to an ingest endpoint are the obvious next sources. `ingest_file` is already the seam for that.
- There's no speaker identification. TV and other voices are filtered by the triage agent's judgment, not by voice ID.
- Triage can't add to an existing dispatched thread yet; it logs continuations instead. `agent-coord send` would be the way to add that.
- If you click Allow on the permission prompt while ramble is set to off, it opens the mic and closes it again right away. That's expected.

## Global instructions

Open **Instructions** in the iPad Companion (reload the Companion once after
upgrading), or choose **Global instructions…** in Ramble's Mac menu. Enter your
preferences, personal context, names, project locations, and preferred location and tap **Save instructions**. Loading, saving and errors are shown
in the editor; unsaved drafts stay in place on a failed save. **Reload saved**
discards a draft after confirmation. Saving a blank editor clears preferences.

The single durable source is `RAMBLE_HOME/instructions.md`, normally
`~/.ramble/instructions.md` (UTF-8, up to 32 KiB). You may also edit that file
on the Mac. Every subsequent fast voice reply and thought triage reads it again,
including either Codex or Claude triage and work-session messages; no restart is needed for edits. These instructions are included in model requests and may be passed to work sessions, so enter only context you want those providers and agents to receive. Relevant
preferences are included in the triage context for dispatch briefs. Set `timezone` (an IANA zone), `whisper_initial_prompt` (recognition vocabulary), `work_session_root`, `triage_cwd` and `jev_api_key_file` in `~/.ramble/config.json`; restart the app after changing those settings. The reminder timezone defaults to the Mac’s timezone when detectable, otherwise UTC. Listening state is separate.

These are runtime user preferences. The repository's `AGENTS.md` exists and
contains development rules; it is separate from this editor. Saved preferences
do not override privacy, routing, ambiguity/consent, pause, dispatch-budget or
structured-output safeguards, and do not request work by themselves. A missing
file means no preferences; an unreadable or invalid file fails the request
visibly in the logs instead of silently dropping the instructions.

The paired Companion session can view and edit these preferences, while report
publishing still requires its separate writer credential. Its source home follows
the Companion's Ramble database selection (`--transcript-db` parent directory,
otherwise `RAMBLE_HOME` / `~/.ramble`). The Companion imports the shared store
from the sibling `ramble` checkout, so keep those checkouts alongside each other.

## Interactive work sessions

Say **“Ramble, let’s investigate why this is slow”** to work together, or
**“Ramble, have an agent audit this and report back”** for an independent task.
Say **“Ramble, start a new session with GPT Astra ultra”** (or **“Astra max”**,
or another available model and reasoning level) to choose that session's settings.
This starts a new foreground conversation without changing saved defaults.
The choice survives a queued launch/restart. If you only open a session, it asks
what you want to work on. Sessions are explicitly in voice mode and instructed
to give short spoken progress every 30–60 seconds between tools during ongoing work.
Persistent work has no 120-second Ramble turn limit; the quick responder's
automatic ultra deadline is 15 minutes. See [model settings](docs/model-settings.md).
A correction or follow-up uses the existing task owner. Say the task/agent name
when changing owners; ambiguous targets are clarified. Ordinary ambient capture
keeps its existing slow-triage behavior. Long duration or coding alone does not
trigger delegation. Behavior is in `prompts/fast.md` and `prompts/work-session.md`.

New work sessions receive the existing task goals and recent public messages so
requests such as “put that in a new session” retain their referent. The exact
spoken request is preserved; previous task context is reference data.

Completed final answers are also published to Companion reports, including when
spoken updates are quiet. “Ramble, put the morning brief in Companion” uses the
structured `publish_result` route to publish the selected session's saved answer
without another worker or regenerating the report. Publication receipts live in
`session_publications`; an unconfirmed publication is not automatically repeated.
Recovered history and stopped-turn output are not published on restart. The
workspace follows new visuals automatically; **Hold this visual** and explicit
history selections keep your place until **Resume live visuals**. Automatic final
answers are archived without replacing an explicitly published visual. Preparing a
brief does not itself schedule a morning notification or a fresh news update.

Open Companion for the unified visual workspace, with conversation history
collapsed below the current artifact. Use its session controls to
send a follow-up, select the foreground voice task, **Stop talking**, or **Stop
work**. Quiet speech does not stop execution. Stop work cancels queued input,
requests interruption of the exact native turn, and shows stopping until the
owner confirms an idle state. Completed side effects cannot be undone by Stop.
Sending new input resumes the same conversation. Native tool approvals remain
in Ribbon Field; **Open conversation** takes you there.

This requires the updated Agent Coord CLI **and** a rebuilt Ribbon Field app.
`ramble/session_backend.py` uses only supported CLI controls. The owning app
runtime starts/steers/interrupts the native conversation; Ramble never attaches
a second native owner. `session-control watch --adopt` explicitly records control
of a named existing Codex app thread. Another interactive controller's binding
cannot be overwritten. These local CLI identities are coordination boundaries,
not an OS sandbox against other code running as the same user.

`ramble/session_store.py` adds work sessions, source-segment claims, command
receipts, a replayable public-event journal and speech jobs to `~/.ramble/ramble.db`.
Commands are saved before submission and use stable native request IDs. Restart
reconciles receipts and resumes observation; uncertain native writes are not
blindly replayed. Interrupted speech is marked uncertain and is not played again.
Stop invalidates queued speech and suppresses late output from the stopped turn.
Only completed public assistant messages appear in conversation history; reasoning
and tool output are excluded. New follow-ups re-read `prompts/work-session.md`,
so existing owners receive the current visual guidance without a new agent. Work prompts put short spoken updates in `[say]…[/say]`.
Speech is serialized, waits for current speech/transcription, coalesces progress,
and gives direct answers priority. The existing playback echo guard is retained:
the user’s speech during playback is still dropped. Voice steering works between
updates; reliable talking over playback needs future echo cancellation.

Defaults: `work_sessions_enabled: true`, `work_session_root: your home directory`,
`work_session_yolo: true` (the existing dispatcher permission policy), and
`work_progress_interval_s: 30`. `work_session_model` and `work_session_effort`
are null, inheriting provider defaults. Set `work_session_yolo: false` for native
approvals. Setting `work_sessions_enabled: false` restores legacy fast/slow
handoff routing. The daily dispatch cap covers both session starts and background
dispatches; zero blocks new workers. Existing sessions can still be controlled.
Reminder/time-entry writes retain their structured adapters and receipt journal.
General work with full shell permissions is not a hard credential isolation boundary.

Verification uses fake native workers, throwaway homes and mocked/disabled speech:
`tests/test_sessions.py`, `tests/test_speech_queue.py`, Companion HTTP/SSE tests,
and Agent Coord `test_session_control.py`. No real microphone or speaker playback
is required. Actual voice timing and physical iPad behavior need an ordinary user
interaction through the installed app.

## Voice and visual workspace

Voice carries the conversation; Companion shows the material needed to understand
and steer the work. The active workspace combines session controls and visual
results. Ask “show me how FooBar works” to see inspected code, annotations or a
diagram while hearing the explanation. Useful visuals are published during work,
not only with the final answer. Spoken progress leaves the visual in place;
conversation history is available separately when needed.

This is a product rule in `AGENTS.md` and runtime guidance in
`prompts/work-session.md`, `prompts/fast.md` and `prompts/triage.md`. Keep all of
these consistent when changing routing, publishing or the Companion interface.
