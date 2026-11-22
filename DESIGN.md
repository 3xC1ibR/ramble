# Ramble interface contract

This records the existing native interface and the controls added on 2026-10-29.
Companion follows its own sibling `ramble-ipad-display/DESIGN.md`.

## Posture and visual rules

The floating pill is an auxiliary status display. It stays above other apps and
never takes keyboard focus. The native app window holds settings, token usage, and article reading.
Activity and its measured waveform remain the pill's primary information.

`ramble/mini_window.py` owns the native size and color tokens: `COMPACT`,
`EXPANDED`, `HOVER_COMPACT`, `HOVER_EXPANDED`, `CONTROL_*`, and `COLORS`.
Reuse its system fonts, SF Symbols, dark capsule, and native popup menus.
The compact idle size is 48 × 22 points; persistent controls appear only on hover.
After a dictation paste, an Undo action replaces the processing dots for six
seconds. It is visible without hovering and uses the existing nonactivating pill.
Keep all controls inside the expanded frame, with the central area draggable.
New ambient work shows its task name and a Stop action for twelve seconds in a
360 × 38 point pill. Hovering recalls the latest task while it remains active;
hover controls add their existing width. Task names truncate within the label
area and remain complete in the tooltip. Stop targets that displayed task and
observed turn through Agent Coord. Dictation, voice interaction and microphone
errors retain their existing precedence. The notice never takes keyboard focus.
Reduced Motion disables decorative animation; measured levels still update.

## Objects and actions

| Object | Actions | Placement |
|---|---|---|
| Ramble capture | Pause for a duration; resume early | Pill pause/play, context menu, app sidebar, menu bar |
| Model settings | Choose model and reasoning | Pill sparkles and native settings |
| Main window | Open | Pill expand arrows and menu bar |
| Mini window | Move, reset position, show/hide | Drag center, context menu, menu bar |
| Dictation | Start/finish and paste; cancel; undo last paste; configure shortcut | Global shortcut; Escape while active; temporary pill Undo; existing Settings page |
| Ambient work | See the task name; stop the displayed task | Temporary pill notice; hover while the task is active |

Pause uses the existing quiet-snooze behavior; it is distinct from the microphone
and voice switches. The pause button becomes play while paused, with matching
accessible labels and tooltips. External changes and automatic expiry update it.
Do not add a new pause state or an additional settings window for these actions.

## Decision log

- 2026-10-30: Added a temporary task-name and Stop view for automatic ambient
  dispatches. Reused the pill, working color, native button styling and supported
  Agent Coord controls. Removed progress dots from this view to make room for
  Stop. Research still starts automatically; explicit dictation stays separate.

- 2026-10-30: Added a six-second Undo action after dictation paste. Reused the
  expanded pill, native button styling, and processing color; removed progress
  dots from the completed state. Expiry, new typing, and a new dictation dismiss
  Undo. It targets the original input and checks available text state first.

- 2026-10-30: Explicit dictation is an action on microphone capture. Its shortcut
  and auto-paste permission use native settings controls. `DICTATION_LAYOUT` in
  `app_window.py` owns their geometry. The pill reuses mint waveform styling for
  “Dictating”, blue processing styling for “Transcribing” and delivery notices;
  it remains nonactivating and exposes full detail on hover. Dictation skips all
  assistant routing and model classification.

- 2026-10-29: Exposed 15-minute, 1-hour, and 3-hour pause on the pill using existing
  quiet-snooze callbacks. Added one control width to hover sizes; idle sizes stay
  fixed. Native bitmap and interaction checks use a throwaway Ramble home.

## Article reading

Reading is an action on an immutable article, with a durable passage position.
The Reading page keeps source text primary and shows explanation in a separate,
labelled area. Preparing is visible immediately through the start control and a
system spinner beside Pause. A playing-color passage highlight only appears when
the audio-start receipt supplies `active_index`. A paused place uses a neutral
system background and explicit saved-position label, never a link-like underline. Previous and
Next select a passage and leave it paused. Resume is explicit after questions.
Explanations belong to the article identity and paused revision; advancing or
replacing the reading clears stale answers. Offline state removes playing color.
Expanding a hidden window opens an active or paused reading. Explicit Settings
or Token usage navigation, and the selected page of a visible window, take priority.

Use native controls, system colors and the `READING_LAYOUT` / `READING_TYPE`
tokens in `reading_view.py`. Follow current is initially on, switches off when
the reader scrolls manually, and never prevents selecting or copying the exact
source. Pasted text and optional source URL stay local; URLs are metadata only.

- 2026-10-31: Added the authorized Reading page with shared persistent controls,
  receipt-driven passage highlighting, and separate questions/recap. Reused the
  existing window and sidebar; the floating panel retains its status role.
- 2026-10-31: Screenshot review refined saved-position styling and added immediate
  native preparation feedback. Offline reading disables playback and questions.
