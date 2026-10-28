# Fast-response diagnostics

## Input-delivery failure

The voice responder polled `Popen.communicate(input=prompt)` with a 0.1-second
timeout, then called `communicate(input=None)` on later polls. If the first poll
expired before all input reached the pipe, Python stopped selecting stdin for
writes. Codex waited for the remainder of the prompt and EOF, while Ramble waited
for a reply. Increasing the deadline did not resolve this deadlock.

The 20,477-byte prompt reconstructed from live conversation context stalled for
60 seconds without emitting a Codex event. Small standalone prompts completed
in 6.7–9.8 seconds. A slow Python reader reproduced the failure with a 128 KiB
input, independently of Codex, networking, microphone or speakers.

`codex_runner.run` now supplies the entire UTF-8 prompt through an anonymous
private temporary file under `RAMBLE_HOME`, independently of output polling.
The file is closed automatically. The same live-context prompt completed in
7.43 seconds after the fix; a 20,564-byte full-context weather replay completed
in 10.33 seconds, with its read-only weather lookup taking about 0.61 seconds.
`test_large_prompt_reaches_slow_reader_after_multiple_poll_timeouts`
checks that a slow reader receives every byte, including Unicode, after multiple
poll intervals and that no prompt file remains.

Low-effort requests retain a **30-second** deadline. The deadline still terminates
the owned process group, including launcher/tool children. Pause and shutdown
cancel the call promptly. Output is capped at 4 MiB during polling, and pipe
cleanup has its own bounded wait. Unknown side effects are never replayed as a
retry. HTTP 429 still fails promptly and respects the server's retry delay.

After `fast_wait_notice_s` (default **12 seconds**), an interactive request queues
one short wait notice. It is background speech and does not open a conversational
follow-up window. A still-pending notice is cancelled when the answer arrives or
fails. The terminal error explicitly names Codex; it is fixed application text,
not a model-generated explanation of the failure.

## Deep-reasoning timeout

One affected request combined 13 accepted speech fragments; the last fragment
alone did not represent the full request. An earlier invocation completed in
23.13 seconds and was superseded by a continuation. The final invocation used
`gpt-6-astra` / `max`, a 29,157-byte prompt, and a 30-second deadline.
It emitted `thread.started` and `turn.started`, then reached that deadline with
no tool or answer events. This is not the earlier stdin-delivery deadlock. The
trace cannot distinguish provider waiting from unreported model reasoning.

`fast_timeout_s` now defaults to null: the chosen effort determines the hard
budget (30 seconds for none/minimal/low, 60 medium, 90 high, 120 xhigh/max/ultra
or unspecified). A positive numeric override remains authoritative. The same
resolved value is passed to the process runner and saved in the trace. Settings
are resolved per invocation, so a live model change gets the matching budget.
Cancellation, the early wait notice, process-group termination, rate-limit
backoff and the prohibition on automatic retries remain in effect.

The investigation also exposed premature success bookkeeping. Background triage
logged ownership before the fast call finished, and Companion's legacy wording
rendered that as “answered directly.” `input_receipts.py` now derives generated
log summaries from claims, updates them after input outcomes, and repairs old
ownership logs at startup. Log notes distinguish pending requests, failures,
queued work and prepared responses without asserting playback or task success.
Repair does not touch mixed thoughts, ordinary triage summaries or dispatches,
and does not replay a failed input. Existing digest entries remain historical
snapshots; current thought records and Companion receive the repaired status.

## Find the evidence

1. Run `ramble show <thought-id>` for the original speech and decision.
2. Find its segment/request ID in `~/.ramble/logs/ramble.log`.
3. Search that ID in `~/.ramble/triage/fast/*.jsonl`. Each invocation has its own
   `call_id` and file, including retries caused by a request continuation.
4. Follow `speech_queued`, `speech_job_start`, synthesis/playback stages and
   `speech_job_end` in the rotating log. Speech workers recover the original
   request ID from the thought; `speech_job_id` links queue and delivery receipts.

For example, substitute IDs from your own private diagnostic record:

```bash
rg -F '<segment-or-request-id>' ~/.ramble/logs/ramble.log ~/.ramble/triage/fast
ramble show '<thought-id>'
```

Each trace records model/effort, deadline, prompt byte count, executable path,
whether an API-key environment variable was present, child PID, elapsed times,
Codex event/item types, tool category and command hash, exit codes, recognized
HTTP/retry diagnostics, token usage when supplied, and the terminal outcome.
`last_event` and `active_tools` identify the last observed stage at timeout.

- Only `process_started`: no Codex event was received; investigate startup/input.
- Thread/turn started with no tool: startup finished; the model request has not
  produced a completed answer. Check error/reconnect/HTTP diagnostics.
- Tool started without completion: the lookup/tool was still active when stopped.
- Completed answer but failed speech receipt: response generation succeeded;
  inspect synthesis and playback.

JSONL is flushed after each event, so partial evidence survives app termination.
Files are owner-readable/writable, limited to 1,000 events plus the terminal
record, and retained for at most seven days or 500 invocations. Prompts, speech,
reasoning, full commands, tool output, raw stderr and credentials are excluded.
Command hashes identify repeated commands without retaining arguments. Arbitrary
error text is reduced to known diagnostic categories; the trace does not claim to
preserve every provider detail. A storage error is logged and does not stop the
answer. A trace without `call_finished` indicates an interrupted process/app.

The event stream follows the official
[Codex JSONL format](https://learn.chatgpt.com/docs/non-interactive-mode#make-output-machine-readable).
These events measure process stages, not audible onset or whether the user heard
the speakers. The Companion's background-triage status is separate from the
interactive input and speech receipts.

## Safe verification

Use a throwaway `RAMBLE_HOME` with `dispatch_daily_cap: 0`, `fast_speak: false`,
`speak_questions: false`, notifications disabled, and the test listening state off.
Tests use fake processes/players or read-only responder lookups. Do not open the
microphone from a terminal, play fixtures on speakers, dispatch workers, or start
a second daemon against the live home.
