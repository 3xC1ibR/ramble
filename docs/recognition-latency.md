# Recognition latency after idle

The transcription worker keeps a bounded amount of MLX memory resident while
Ramble listens. This addresses slow first decodes after macOS has paged out the
recognizer during an idle period. It also caps MLX's cache of unused buffers.
The model, recognition hints and full unhinted name-confirmation pass are unchanged.

The original investigation found a 19.36-second recording that took 13.21 seconds
to transcribe live and 1.65–1.71 seconds when decoded again with a warm model.
The 16 GB M1 Pro was using 11.6 GB of swap, and Ramble's memory map reported
2.6 GB swapped. Memory pressure was a supported explanation, but the historical
log did not contain enough detail to establish the exact cause of that stall.

## Memory policy

Defaults in `ramble/config.py`, overridable in `RAMBLE_HOME/config.json`:

| Setting | Default | Behavior |
| --- | ---: | --- |
| `whisper_wired_mb` | 2048 | Resident-memory budget in MiB; capped at one quarter of physical RAM and Metal's recommended working-set limit. Zero disables residency. |
| `whisper_cache_mb` | 128 | Unused MLX buffer cache budget in MiB; capped at one sixteenth of physical RAM. `null` keeps MLX's default; zero disables the cache. |
| `whisper_residency_refresh_ms` | 1000 | Native Metal residency renewal interval, bounded to 100–60000 ms. Zero disables renewal. |

Restart the app after changing settings. The refresh environment variable is set
before MLX initializes its Metal device. MLX 0.32.4 in this project's environment
supports it. The native renewal thread submits no model inference or GPU compute.
Ramble does not repeatedly decode dummy audio while idle.

Turning the mic off or selecting a timed pause releases the resident reservation
on the transcription worker's next poll (within five seconds when idle). Pending
recordings may finish first. Listening or a new decode restores the budget.
Worker shutdown restores the previous MLX limits. These actions do not write the
microphone switch. Unsupported memory controls log a warning and allow ordinary
pageable transcription to continue.

The budget is an upper bound, not an assertion that every model buffer is resident.
It does not limit total process memory or all peak inference allocations. Larger
models may not fit within it. These controls are process-wide; the single
transcription worker is Ramble's owner of MLX.

To return to the earlier memory policy:

```json
{
  "whisper_wired_mb": 0,
  "whisper_cache_mb": null,
  "whisper_residency_refresh_ms": 0
}
```

## Diagnostics

`ramble.latency` adds `transcription_decode_start` and `transcription_decode_end`
for `warmup`, `hinted`, `confirmation` and `unhinted` passes. Events include the
source segment ID, audio duration, time since the previous decode, elapsed time
and success status. `transcription_decode_memory` records MLX active/cache/peak
allocation sizes and process page-fault deltas. Process counters also include
other threads; they do not measure GPU page-ins directly. No transcript or audio
content is included in these events.

`transcription_memory_configured` records the selected limits;
`transcription_residency` records reservation changes. Allocation counts and the
configured budget alone do not prove physical residency: compare the idle memory
map and first-request timing too.

## Silent validation

`bin/benchmark-transcription` decodes an existing 16 kHz file directly. It does not
start a daemon, open the microphone, play sound, call a responder or dispatch work.
Use a throwaway `RAMBLE_HOME` whose `config.json` contains:

```json
{"dispatch_daily_cap": 0, "fast_speak": false, "speak_questions": false}
```

Run each variant in a separate process, sequentially, so they do not compete for
model memory. Use the same saved recording and similar system load. For example:

```sh
RAMBLE_HOME=/tmp/ramble-test .venv/bin/python bin/benchmark-transcription \
  /path/to/recording.flac --baseline --idle-s 600 --cycles 1 \
  --output /tmp/ramble-test/baseline.jsonl
RAMBLE_HOME=/tmp/ramble-test .venv/bin/python bin/benchmark-transcription \
  /path/to/recording.flac --idle-s 600 --cycles 1 \
  --output /tmp/ramble-test/managed.jsonl
```

The tool refuses the default live home and repository home/output paths. It requires
safe test settings, uses already-cached models by default, and refuses to
overwrite a result file. It checks every transcript against an adjacent `.txt`
file when present, otherwise against the first decode. Results include per-pass
traces, complete request times, idle CPU usage, and transcript hashes.

Inspect `vmmap -summary <benchmark-pid>` during idle to compare swapped model
memory. Use longer idle windows and ordinary real load when validating the
reported multi-second stalls. Warm timings alone cannot validate this change.

[historical text removed for privacy]

[historical text removed for privacy]
[historical text removed for privacy]
The two recognition passes and exact transcript remained unchanged. Tests used
separate processes and a throwaway home, without recording or playing audio.

The repeat baseline ran without an app restart. Both variants used the same
benchmark and recording under ordinary system load:

| Measurement | Previous policy | New policy |
| --- | ---: | ---: |
| Complete recognition after ten idle minutes | 17.999 s | 2.208 s |
| First recognition pass after idle | 16.976 s | 1.320 s |
| Confirmation pass after idle | 0.964 s | 0.837 s |
| Immediate warm repeat | 1.608 s | 1.668 s |
| Cached unused buffers after recognition | 917.5 MiB | 86.0 MiB |
| CPU time during ten idle minutes | 0.048 s | 0.111 s |

That is 15.791 seconds, or **87.7%**, less recognition latency in this comparison.
It is one valid ten-minute comparison on a loaded machine, not a percentile or
a guarantee for every interaction. Warm performance and transcript output were
preserved.

The new policy's warm requests before idle took 1.629 and 1.661 seconds. MLX
reported 1543 MiB active allocations and 86 MiB cached buffers. During idle,
`vmmap` reported 1.6 GiB dirty graphics
buffers and 16.2 MiB swapped, versus 0 dirty and 2.4 GiB swapped in the baseline
repeat. These are memory-map observations, not guarantees for other loads.

The initial baseline took 12.628 seconds after ten minutes idle and 1.650 seconds
on the immediate repeat. Another work session restarted the installed app at
that first post-idle decode, so that timing is excluded from the clean comparison.

All 24 focused memory-policy, transcription, latency and runtime tests passed.
They cover hardware bounds, unsupported controls, off/resume/shutdown behavior,
unchanged name confirmation, failure diagnostics and the single-daemon guard.
The benchmark also rejects a repository-local test home before creating files.

[historical text removed for privacy]
[historical text removed for privacy]
[historical text removed for privacy]
[historical text removed for privacy]
[historical text removed for privacy]
[historical text removed for privacy]
[historical text removed for privacy]

[historical text removed for privacy]
[historical text removed for privacy]
[historical text removed for privacy]
[historical text removed for privacy]
These measurements isolate recognition time; they do not measure the subsequent
responder, synthesis or speaker playback delay.

Upstream references: [MLX wired memory](https://ml-explore.github.io/mlx/build/html/python/_autosummary/mlx.core.set_wired_limit.html),
[cache limits](https://ml-explore.github.io/mlx/build/html/python/_autosummary/mlx.core.set_cache_limit.html),
and [native idle residency renewal](https://github.com/ml-explore/mlx/pull/4633).
