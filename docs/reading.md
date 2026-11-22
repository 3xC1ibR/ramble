# Persistent article reading

Reading keeps the original article, its saved position, and discussion separately.
The native app's **Reading** page and Companion's workspace show the same source
and playback state. Paste an article in either interface, or ask a Ramble work
session to retrieve it and start the reader.

The read-along highlights the passage whose audio has started. While audio is
being prepared, the interface says so without advancing the highlight. Pause
retains the interrupted passage; Resume starts that passage again. Previous and
Next move the saved place and leave reading paused. Following the current passage
can be disabled to browse elsewhere in the text. Highlighting is at passage
granularity, not estimated word timing.

**Where were we?** gives a brief recap of confirmed completed passages. **Ask**
answers from the article while keeping narration paused. The explanation appears
separately from the original text. Continue remains explicit. Seeking past a
passage does not mark it as heard. A new article, seek, resume, or newer question
invalidates a late answer and prevents its audio from playing over the new state.

## Voice and command line

Once the microphone can hear the user, addressed controls such as “Ramble, pause
reading”, “Ramble, keep going”, “Ramble, read that again” and “Ramble, where were
we?” run locally without waiting for the response model. Ordinary addressed
requests near an active reading include the source in the existing fast
responder's context. The model distinguishes article questions from unrelated
requests; answering a source question does not require a second model call.

The existing playback echo guard still discards microphone audio while Ramble
speaks. Use the visible Pause control during narration, then ask by voice, or use
the app's question controls. There is no automatic phone-call detector or claim
of echo-cancelled spoken interruption. Global voice mute and quiet snooze pause
reading; clearing them does not unexpectedly resume the article. Microphone
preferences remain independent.

```sh
ramble reading start --file /path/to/article.txt --title 'Article title' \
  --source-url https://example.com/article --request-id unique-reading-request
ramble reading status
ramble reading pause --reading-id READING_ID
ramble reading recap --reading-id READING_ID
ramble reading ask 'Why does the author make that claim?' --reading-id READING_ID
ramble reading resume --reading-id READING_ID
ramble reading seek 4 --reading-id READING_ID
```

`start` also accepts `--stdin` or `--text`. Source URLs are metadata; the reader
does not fetch them. Work-session agents retrieve and extract the actual article
before calling `start`; they do not send it as one long spoken progress update.
The command receipt confirms saved/queued state, not audible delivery. A stable
request ID prevents a transport retry from restarting a reading. Supplying the
reading ID prevents an old view from controlling a newly selected article.

## State and delivery

`ramble/reading.py` owns `ReadingStore` and `ReadingController`. Runtime state
belongs under `RAMBLE_HOME`, never in the checkout. The controller runs inside
the installed daemon and shares the existing serialized speaker output. The
UI and CLI never start another player or microphone pipeline.
The store and question API use only the Python standard library; audio packages
load only when the daemon creates its playback controller.

Only confirmed output completion advances position or marks a passage heard.
An interrupted passage remains the next passage. An app restart preserves its
place and requires explicit Resume. Generation checks prevent a synthesis or
playback callback from an old selection from changing a new selection. The
controller heartbeat is independent of source revisions.

The first short passage is synthesized promptly. Bounded preparation of upcoming
passages overlaps current playback; prepared audio can be reused after a pause.
Cached current audio bypasses pending speculative generation. A separate bounded
worker prepares the current passage so old prefetch calls cannot block a new one.
The reading worker yields to existing response work. Preparation and playback
remain different states, so UI feedback is immediate without pretending the
speaker has started.

`reading_interactions.sqlite3` stores question requests and separate answers.
The question worker uses the configured fast model with a read-only request;
article text is reference data. Queued requests survive restart. Interrupted
model calls become explicit failures rather than silently replaying. UI changes
remain responsive while a question is being answered.

## Validation and latency evidence

Tests use isolated homes, fake synthesis and output receipts. They never play
speaker audio, open the microphone, or dispatch work agents. Focused suites are
`test_reading.py`, `test_reading_interaction.py`, `test_reading_view.py`, and the
Companion `test_reading.py`, plus existing speech/routing/snooze regressions.

A silent provider probe on 2026-10-31 used the configured ElevenLabs voice and
speed, generating files without playback. Three paired trials measured:

| Input | Generation times (seconds) | Median | Generated audio |
|---|---|---|---|
| Opening sentence, 71 characters | 0.659, 0.456, 0.597 | 0.597 s | About 3.5 s |
| Full sample, 1,367 characters | 2.057, 1.924, 3.631 | 2.057 s | About 88 s |

These measurements support a short first passage. They are synthesis timings,
not microphone-to-speaker latency, physical speaker timing, or a guarantee for
other articles and network conditions.

Independent silent checks also measured exact-source segmentation of a
190,000-character article at about 19 ms after removing repeated prefix scans
(previously about 17 seconds). A gated synthesis fixture confirmed that cached
resume proceeds while both obsolete prefetch requests remain blocked. These
checks measure local preparation and scheduling, without physical audio output.

Release verification on 2026-10-31 passed the full Ramble suite (544 tests) and
Companion suite (91 tests). A deployment check then exposed an eager audio import
in Companion's standard-library runtime. After isolating that import inside the
playback controller, all 67 reader tests passed, including fresh Python 3.9 and
3.12 processes with site packages disabled. An actual-store HTTP flow also passed
under Companion's Python 3.9, as did its seven reading endpoint tests.

Both installed services were restarted and verified: controller heartbeat live,
paired read API returning 200, unpaired access returning 401, and read-along assets
served. Microphone preferences were unchanged and no new service errors appeared
after the final restart. Native and narrow/wide Companion screenshots received
independent visual review. Physical speaker output and phone-call interruption
were not exercised by these silent checks.
