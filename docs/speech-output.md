# Persistent output for spoken replies

Spoken replies previously invoked `/usr/bin/afplay` for every generated file.
The recognizer residency fix does not remove that separate process/device-open
step. Speech now uses the same persistent output stream as acknowledgement and
working cues. Synthesis providers, voices, response text and request routing are
unchanged.

## Delivery

`speech.play_audio` commits the bounded microphone feedback guard before passing
the file to `cue_player.shared_player()`. The existing cross-process
`speech.lock` still serializes access to speakers. An in-process lock serializes
the shared helper's protocol, so cue supervision cannot consume a speech receipt
or reset its stream in the middle of a reply.

The helper decodes the local file and resamples it to the device's output rate
before publishing an immutable buffer to the callback. Native-rate stereo is
preserved; mono is copied to the output channels. Stereo is averaged for a mono
device. SciPy's polyphase resampler preserves duration and pitch with low-pass
filtering. The decoder and resampler load during startup, before readiness is
announced. SciPy was already installed transitively and is now a direct locked
dependency; no dependency versions changed.

The callback fills silence between jobs. It performs no file reads, resampling,
logging, locks or pipe I/O. Variable-size callbacks copy each frame once, and
success is reported only after the final frame's device playback timestamp.
Working cues end on the first-frame receipt, rather than before opening a player.

Ramble's cue worker supervises and warms the shared helper while voice output is
allowed and either `fast_speak` or `speak_questions` is enabled. Turning off cues
alone does not disable speech warming. Mute or timed pause closes output;
resuming opens a new stream. Microphone on/off state is not written. CLI and
standalone webhook processes have their own helper, with the existing shared
speaker lock; a one-shot CLI invocation cannot retain a helper after it exits.

## Failure and cancellation

The existing callback health watchdog and default-device monitor recover a
stalled, crashed or disconnected output. Speech can wait for readiness before
submission. Once its command has been submitted, a lost result or device change
fails that reply without retrying it: some words may already have been heard.
The next reply can start on a recovered helper. Short cues retain their bounded
retry and two-second freshness policy.

The speech loop checks mute, pause, caller cancellation, shutdown and timeout
every ten milliseconds between protocol polls. Cancellation terminates the
helper, including queued output; the hardware buffer can still impose a small
delay. Pause/mute boundaries remain effective after immediate resume, preventing
old speech from restarting. The feedback guard retains its configured tail on
success, cancellation or failure. Delivery errors propagate to existing
played/failed/cancelled receipts. No fallback replays the file with `afplay`.

## Diagnostics

`playback_command_start/end` remain available and identify the backend as
`persistent_stream`. A new `playback_first_frame` event carries:

- `first_frame_mono_s`: estimated first-frame output time, mapped from the
  PortAudio stream clock into the parent's monotonic clock domain.
- `output_start_delay_s`: delay from submission preparation to that estimate,
  including any wait for helper readiness and file conversion.
- `timing_source: portaudio_dac_estimate`: explicitly distinguishes this from
  sound measured at a microphone or from the first non-silent syllable.

Events preserve request and playback IDs without logging speech text. A shared
helper PID in `audio output ready` confirms that later replies reuse the stream.

[historical text removed for privacy]

128 focused tests passed without speaker playback, microphone capture or agent
dispatch. Tests used throwaway homes; the helper protocol tests ran the real
helper in child processes with only the sound device and CoreAudio discovery
replaced. All output went to NumPy buffers.

Coverage includes:

- One warmed helper reused across cues and 22.05, 24, 44.1 and 48 kHz speech.
- Preserved sample order, stereo channels, pitch and duration; no dropped tail
  and no premature success before the last device timestamp.
- Speech longer than the short-cue timeout.
- Hung startup, callback stalls, underflow, device changes and lost parent pipes.
- No automatic replay after uncertain or partial speech; recovery for the next
  independent reply.
- Mute, timed pause, immediate resume, caller cancellation and app shutdown.
- Feedback guards, follow-up receipts, cue behavior, webhook delivery and
  single-daemon behavior.

Commands (all run with `RAMBLE_HOME=/tmp/ramble-test`):

```sh
.venv/bin/python -m unittest discover -s tests -p 'test_speech_output.py'
.venv/bin/python -m unittest discover -s tests -p 'test_cue_player.py'
```

The other 109 tests are in `test_speech.py`, `test_cues.py`,
`test_delivery_webhook.py`, `test_latency.py`, `test_speech_queue.py`,
`test_snooze.py`, `test_voice_control.py`, `test_runtime.py`, and `test_fastlane.py`.
Warm start timing assertions exercise the protocol with a simulated output
device; they are not measurements of audible latency at the Mac's speakers.
No new claim about end-to-end response latency is inferred from them.

[historical text removed for privacy]
[historical text removed for privacy]
[historical text removed for privacy]
helper exited. Both microphone and voice-state file hashes were unchanged.
[historical text removed for privacy]
[historical text removed for privacy]
[historical text removed for privacy]

References: [sounddevice callback and DAC timing](https://python-sounddevice.readthedocs.io/en/0.5.6/api/streams.html),
[SciPy polyphase resampling](https://docs.scipy.org/doc/scipy/reference/generated/scipy.signal.resample_poly.html).
