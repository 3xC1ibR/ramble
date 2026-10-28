# Action routing and execution receipts

Accepted timer and reminder requests use Ramble's structured adapters.
`action_routing.py` derives supported operations from the adapters' output
schemas and supplies the same capability list to quick replies, thought triage
and work sessions. Command recognition excludes quoted examples, tentative
speech and requests to build or debug timer software. It selects an executor;
it does not authorize speech or invent arguments.

For recognized commands the responder schema permits only the selected adapter
operation or a question for missing arguments. Worker routes and legacy handoffs
are excluded, and returned actions are checked again before execution. Unsupported
operations report their limitation. Recognition is conservative, not a guarantee
for every possible phrasing; shared prompt guidance applies beyond these patterns.

## Internal transfers

- An admitted voice request reaching `Sessions.route` transfers to the action
  queue with its original segment IDs, without starting a worker.
- Companion timer/reminder messages transfer before native submission, retaining
  their command ID and session epoch. Stop cancels queued session actions.
- Triage recovers a missed, explicitly named action from its original segment
  only while it is at most three minutes old and allowed by pause state. Its
  source claim and queue entry commit together. Passive or older speech requires
  authorization, not repetition of the request.

The fast worker consumes internal transfers separately from live speech
coalescing. Transfers retain their identity across restart and cannot replay
completed or uncertain writes. The existing adapter journals provide external
write receipts. Historical requests are not automatically retried.

## Outcomes

`RAMBLE_HOME/ramble.db` holds `action_requests`: original IDs/text, operation,
optional session/epoch, status, result text and timestamps.

| Status | Meaning |
| --- | --- |
| pending | Accepted into the action queue |
| running | Execution started; completion not confirmed |
| completed | Adapter returned a confirmed result |
| needs_input | An argument or target needs clarification |
| failed | Execution rejected or could not be prepared |
| unconfirmed | Execution may have occurred; no automatic replay |
| cancelled | Cancelled before execution |

Interrupted running actions become unconfirmed on startup. Receipts are saved
before playback; muted or failed speech cannot invalidate them. Companion reads
receipts through its existing live stream and on reconnect. Explicitly selected
sessions show only their receipts; automatic selection shows the latest action,
including those needing no worker. An older timer task without a receipt is
unconfirmed. **Conversation idle** carries no success checkmark.

## Triage evidence

Both classifier backends preserve the exact policy, dynamic context, combined
input, SHA-256, output schema, status and captured outputs. Input is saved before
invocation and retained on timeout. Claude's policy and context remain separate
invocation inputs and are separately recorded. `input` now includes both;
`context_input` preserves the former dynamic context. Historical audits cannot
recover policy that was never saved.

## Validation

Use a throwaway `RAMBLE_HOME`, dispatch cap zero and disabled speech/cues.
`tests/test_action_routing.py` covers command boundaries, strict capabilities,
worker misrouting, unsupported operations, original-identity transfers,
Companion recovery, Stop, duplicates and uncertain outcomes. The triage audit
test checks both backends before invocation and after timeout. Companion's
`tests/test_action_outcomes.py` verifies receipt isolation, SSE and reconnects.
Executors and native agents are fake; tests do not change real timers.
