# Voice steering

While a foreground Codex work session is thinking or using tools, say
**“Ramble, actually focus on the tests first”** or **“Ramble, keep the existing
design.”** The follow-up stays with that conversation. You do not need to wait
for its first reply or stop its work.

An unnamed correction can also reach the same owner within the follow-up window
(one hour by default), after a read-only address check. Background speech and
ambiguous thinking aloud stay passive. Explicit owner switches and structured
reminder/time-entry actions retain their usual routing.

`Sessions._process` captures the native active turn before submission and saves
the command durably. `SessionBackend.command` passes it to the supported
`agent-coord session-control submit --expected-turn-id ID` interface. The owning
Ribbon Field runtime sends Codex `turn/steer` with that exact turn ID. If the
conversation is idle, the same voice path starts a new turn in that conversation.
Steering does not create another agent or interrupt a running tool. Native
Stop work remains a separate control.

The native steering connection already existed. The voice-steering update makes
the runtime prompts explicit about applying corrections during work, including
before an initial reply, and adds transcript-to-CLI regression coverage.

The playback echo guard still drops microphone speech while Ramble speaks.
Speak between spoken updates, or disable spoken replies while keeping the mic
on. Talking over playback needs echo cancellation.

## Validation

`tests/test_voice_steering.py` exercises transcription admission, direct and
classified follow-ups, the durable command queue, and the real SessionBackend
CLI argument builder against a simulated CLI. It checks active-turn IDs, idle
continuations, restart reconciliation and duplicate prevention. No Codex agent,
microphone or speaker playback is started.

Codex's native contract is documented in the
[official OpenAI App Server documentation](https://learn.chatgpt.com/docs/app-server#steer-an-active-turn).
