# Source discrimination and daily transcript notes

Ramble's microphone hears everything in the room: the owner, other people,
television, YouTube, radio, music, and Whisper's own repetition artifacts. In
November 2026 the owner's own speech was about 2% of transcribed characters in
a blind sample of 60 thoughts; 65% was media. Routing and notes are only useful
after those sources are told apart. Everything here is **off by default**.

## Components

| Module | What it does | Cost |
|---|---|---|
| `ramble/speaker_embedding.py` | WeSpeaker ResNet34 (VoxCeleb, ONNX) owner-voice embedding; Kaldi filterbanks in numpy, matching `torchaudio.compliance.kaldi.fbank` (cosine 1.000000 on real clips) | ~0.2 s CPU per clip, 2 threads, no new dependency |
| `ramble/output_activity.py` | Samples, once a second, which *other* processes send audio to an output (`kAudioProcessPropertyIsRunningOutput`, macOS 14.4+). Read-only; no capture or tap | negligible |
| `ramble/source_gate.py` | Per-segment verdict from artifacts, owner score and playback; cached in `segment_sources`; optional local "ambient" triage decision | — |
| `ramble/transcript_route.py` | One HTML document per local day of the owner's words, from configured classes; files by default, or JSON on stdin to a configured command | — |
| `contrib/stoic_note_sink.py` | Example delivery command: one user-authored Stoic note per day, replaced with `If-Match` on re-delivery. Not imported by Ramble | — |

### Gate rule (fail open)

```
artifact (Whisper loop: gzip ratio > 2.4, or vocabulary-hint echo)  -> remove
no score (no profile, model missing, scoring error)                  -> keep
score >= threshold                                                   -> keep
another process was playing audio for >= half the segment            -> remove
segment shorter than 2 s                                             -> keep
otherwise                                                            -> remove
```

Short clips are kept unless the Mac was playing audio because short owner
commands score poorly; that was the main failure of the earlier shadow filter.

## Measured accuracy (owner's own listening labels, held out)

Enrollment: 200 shortcut-dictation clips (always the owner). Threshold: the
score that keeps 90% of 39 labeled owner clips from an earlier, separate
period (0.204). Test sets were not used for enrollment or threshold.

| Test set | Owner speech kept | Non-owner removed |
|---|---|---|
| Human labels, 67 clips (calls, a movie) — WeSpeaker | 39/41 (95%) | 21/26 (81%) |
| Same, never cutting clips under 2 s | 40/41 (98%) | 18/26 (69%) |
| November silver set, 434 clips (dictation vs TV and radio) | 256/262 (98%) | 157/172 (91%) |
| Same, never cutting clips under 2 s | 262/262 (100%) | 141/172 (82%) |
| Baseline: frozen shadow filter + 2 s rule, human labels | 38/41 (93%) | 23/26 (88%) |

The baseline (diarization + transcription models) is comparable in accuracy
but ran in batches, fell 398 minutes of audio behind, and stopped producing
decisions for eleven days without an error. The embedding gate scores each
segment once, in about a fifth of a second.

Known misses: 30-second segments where several television voices are mixed
(scored as a whole clip), the far side of phone calls played through the
laptop speaker, and owner clips under 2 s. Output activity cannot see a TV,
iPad or phone in the room, or a person present; those rely on the voice score.
The other-voice evidence comes from few events (one call, one movie, one sitcom
episode, one radio programme), so confidence intervals are wide.

## Operation (not enabled by this change)

```sh
ramble owner-voice enroll          # from recent shortcut dictation; writes RAMBLE_HOME/owner-voice.json
ramble owner-voice status
ramble output-activity             # is another process playing audio right now?
ramble transcript-note --date 2026-11-21            # dry run: RAMBLE_HOME/transcript-notes/<date>.{html,json}
ramble transcript-note --date 2026-11-21 --deliver  # runs transcript_route.deliver_command
```

Configuration (`RAMBLE_HOME/config.json`):

```json
{
  "output_activity_monitor": true,
  "source_gate_triage": true,
  "transcript_route": {
    "classes": ["log", "actionable", "dictation"],
    "deliver_command": ["python3", "/path/to/ramble/contrib/stoic_note_sink.py"]
  }
}
```

`source_gate_triage` gives a thought a local `ambient` decision, with no Jev or
Codex call, when none of its segments is verified as the owner (unverified
segments fail open to normal triage). Replayed over 2026-11-16..21 it would have
skipped 94 of 191 triage calls: 2.21 M of 4.54 M Codex tokens (49%), plus about
half of 0.87 M Jev tokens. Voice commands are never gated: the fast lane and
dictation paths are unchanged.

The Stoic adapter sends `"author": "user"` (Stoic's memory service indexes only
user-authored notes), never includes Ramble-written summaries, and keeps a map
of day to note ID in `~/.config/stoic/ramble-note-ids.json`.
