# BiHome Wearable — Human-Technology Interaction

*Design analysis of usability for psychology researchers*

---

## 1. Who uses this software

BiHome Wearable is built for a specific user profile:

- **Primary users** — psychology researchers (PhD students, post-docs, PIs)
  running experimental sessions on participants. They are domain experts in
  human behavior but **not necessarily** in Python, Bluetooth stacks, or
  signal processing. Their goal is to *collect clean physiological data
  aligned with experimental events*, not to debug LSL streams.
- **Secondary users** — research assistants who run scheduled sessions
  with minimal supervision. They may have been trained on the GUI in a
  single 30-minute walkthrough.
- **Indirect users** — participants in the experiment. They never touch
  the software, but the experience of being instrumented (waiting for
  setup, sensors falling off, weird noises from the laptop) directly
  affects data quality through movement artefacts and stress.

The system is deliberately optimised for the **first** group. Many
design decisions trade off internal flexibility for surface predictability,
because a researcher staring at a frozen progress bar five minutes before
a participant arrives needs *certainty*, not options.

---

## 2. The HTI problems we set out to solve

Standard tools for multi-sensor acquisition in psychophysiology
(LabRecorder + the manufacturers' own apps + custom scripts) leave the
researcher with a fragmented workflow:

| Step | Tool fragmentation | Failure modes |
|---|---|---|
| Discover device addresses | Windows Bluetooth panel + OS-specific tricks | Wrong MAC → silent connection failure |
| Configure each device | Vendor app per brand (Polar Beat, EmotiBit Oscilloscope) | Inconsistent UI, easy to forget devices |
| Start LSL streams | Bespoke Python scripts per session | Crashes mid-session, no resume |
| Synchronise visualisations | Multiple windows, manual tiling | Researcher loses track of which window = which participant |
| Mark events | Separate marker script, terminal commands | Markers land at wrong timestamps |
| Record | LabRecorder GUI | Forgotten REC clicks → silently no data |
| Inspect data | Export pipeline, MATLAB/Python | Errors discovered hours after session |

Each of these is solvable individually, but a researcher under time
pressure won't solve them well. **BiHome's HTI thesis is that
consolidating these steps behind one wizard and one viewer reduces
preparation time, error rate, and cognitive load enough to materially
improve data quality.**

---

## 3. Design principles

The five principles below guided every UX decision in the codebase.
They are listed in priority order — when they conflict, the higher one
wins.

### 3.1 Zero-config first launch

A naïve user, double-clicking the exe with no prior knowledge, should
reach a working state within five minutes. The wizard is therefore
**mandatory and stateful**: it remembers last session's choices in
`wizard_defaults.json` so repeat launches are one-click. First-time
users see the **Add Device** button prominently — they don't need to
edit JSON files, run setup scripts, or read documentation to get started.

**Implementation:** `wizard_defaults.json` schema-versioned for forward
compatibility; first-launch detection skips the BLE scan entirely if the
registry is empty (no point scanning for devices the user hasn't told us
about yet — it would just waste 6 s and surface unrelated Bluetooth gear).

### 3.2 Errors visible, recoverable, never silent

The worst failure mode in research software is a session that *seemed*
to record but produced empty / misaligned files. BiHome therefore:

- **Sentinels for gaps.** When a Polar drops out for 2 s, we push
  `NaN` samples to LSL rather than letting the timestamp axis stretch.
  Downstream analysis can detect gaps, but the wall-clock alignment is
  preserved.
- **Visible state in the viewer.** A flat-lined channel is unmistakable;
  a stretched-time channel is easy to miss. The choice is HCI-driven,
  not just a numerics fix.
- **Per-session log file.** `%APPDATA%/BiHome/acquisition.log` is
  overwritten each session, with timestamped entries for every device
  state transition. The researcher can attach this to a bug report
  without remembering what they did.
- **Retry semantics.** A failed Polar connection retries automatically
  (with exponential backoff), counts consecutive failures, and only
  gives up after a configurable threshold. A user who toggled Bluetooth
  while the wizard was scanning doesn't need to restart the app.

### 3.3 Per-session, per-user, per-machine isolation

Multiple researchers may share the same lab PC. Sessions across
participants must not leak state.

- All writable state lives in `%APPDATA%/BiHome/`, scoped to the Windows
  user. Sharing a lab PC with multiple Windows accounts gives each
  researcher their own device registry.
- Recordings are timestamped with millisecond precision in filenames,
  preventing accidental overwrites.
- The `active_participants.json` bridge file is rewritten at the start
  of every session — a stale viewer cannot accidentally attach to last
  session's streams.

### 3.4 "If you don't know X, scan for X"

Researchers should not be asked to *know* low-level details (Bluetooth
MAC addresses, EmotiBit serial numbers, IP addresses) when the system
can discover them. Two examples:

- **BLE scanner in Add Device dialog.** The user picks the Polar from
  a list of nearby devices labelled by name. No copying hex strings from
  Windows Settings.
- **EmotiBit dual scan.** First UDP broadcast on the local network
  (fast, ~2 s), then BrainFlow's slower discovery as fallback. The
  researcher just clicks "Scan" — the dual strategy is invisible.

When the system *cannot* discover (EmotiBit serials are printed only on
the PCB), the field is validated *at registration time* (`MD-V` prefix
regex) so the researcher catches typos before the session, not during.

### 3.5 The researcher controls timing, the software does not

Markers in BiHome are stamped at **click time**, not at the time the
backend gets around to flushing them. The marker stream uses
`pylsl.local_clock()` captured in the button handler, and the
inter-thread queue is drained on a 100 Hz timer. This means a click
during a 30 ms Python GIL stall still lands at the user-perceived
moment, not 30 ms late.

This is an HTI decision dressed as a numerical one: psychology
experiments typically tolerate ~30 ms imprecision on stimulus
timestamps, but participants and researchers cannot tolerate
*invisible* timing skew — it destroys trust in the data.

---

## 4. The wizard: cognitive load and progressive disclosure

The setup wizard is the highest-friction interaction. It is also the
*only* moment where the researcher faces choices that cannot be undone
mid-session (number of participants, device assignments). Its design
reflects this asymmetry.

**Step 1 — How many participants? (1–6).**
A single slider/spinbox, no defaults beyond 1. The maximum 6 is a soft
limit chosen because:
- Two Polars in BLE coexistence beyond ~6 starts to drop packets on
  Windows.
- Six participant windows tile cleanly on a 1080p display.
- Field studies in psychology rarely run more than 6 simultaneous
  participants.

**Step 2 — Device discovery.**
Scans appear *simultaneously* with the wizard advancing — the user is
never blocked watching a progress bar while doing nothing. If discovery
fails entirely (Bluetooth off, no WiFi), the user can still proceed and
add devices manually.

**Step 3 — Device assignment.**
Drag-free: each participant gets a dropdown per device type, populated
from the (now discovered) registry. The dropdowns are pre-filled with
last session's assignments when possible — repeat sessions become
one-click.

**Hidden complexity.** What the wizard does *not* expose:
- BLE coexistence ordering (Polars connect sequentially with delays).
- EmotiBit BrainFlow port allocation.
- LSL stream metadata configuration.
- Clock sync EMA parameters.
- LabRecorderCLI subprocess management.

These are decisions the researcher should not be making.

---

## 5. The viewer: parallel visual processing for parallel sessions

When five participants are instrumented simultaneously, the researcher's
attention is the limiting resource. The viewer is designed for **rapid
peripheral-vision sanity-checking**, not deep analysis.

### 5.1 One window per participant

A single shared window with selectable channels would be more
information-dense but forces serial attention. The viewer instead opens
**one floating window per participant**, each labelled with a coloured
header derived from the stream prefix (`P01_`, `P02_`, …). The
researcher can:

- Tile windows across multiple monitors (one window per participant).
- Glance at each in turn during a stimulus block.
- Re-arrange freely — window positions are saved in
  `viewer_settings.json` per machine.

### 5.2 Channels stacked, not overlaid

Each channel (ECG, ACC X/Y/Z, PPG, EDA, temperature, IMU axes) gets its
own row in a vertically stacked plot. Y-axis ranges are per-channel and
auto-scaled with a 95th-percentile envelope. This avoids the trap of
overlaid plots where ECG (millivolts) and ACC (g) become unreadable
together.

A flat-lined or saturated channel is immediately visible in its row.

### 5.3 The REC checkbox: intentional, not automatic

A frequent failure in research is "we forgot to press record." A
tempting fix is to auto-record. We rejected this because:

- Auto-record consumes disk space during setup and breaks.
- Researchers often need to discard the first 30 s of warm-up data;
  starting recording on demand cleanly separates *setup* from
  *acquisition*.
- A user-initiated REC click creates a clear mental model: "the data
  starts here."

Instead, the REC checkbox row sits in the viewer's left panel,
**visible at all times during the session**. A red "STOP" replaces the
button when recording is active, with elapsed time displayed. There is
no way to forget you're recording, and no way to forget you're not.

### 5.4 Marker buttons in the viewer (not a separate window)

Event markers (stimulus onset, task start, etc.) are pressed dozens of
times per session. Asking the researcher to alt-tab to a marker app is
non-trivial latency *and* an attention switch they can ill afford.

Marker buttons therefore sit *in* the viewer, configurable per
experiment, colour-coded by category (event = green, state = blue),
with click-time LSL timestamps as described above.

---

## 6. Multi-participant ergonomics

The shift from single-participant to multi-participant acquisition is
where most existing tools force researchers into custom scripting.
BiHome handles this through a few small but compounding decisions.

**Stream naming convention.**
Every LSL stream is prefixed `P01_`, `P02_`, …, `Pnn_`. The convention
is enforced by the backend (it's not a user-visible field) and parsed
by the viewer via `active_participants.json`. Downstream analysis
pipelines (MNE-Python, FieldTrip, custom) can group streams by prefix
without manual labelling.

**One marker stream, all participants.**
A single shared marker stream is broadcast to LSL, ensuring that all
participants share a common event timeline. The alternative — one
marker stream per participant — would force the analyst to re-align
timelines post-hoc, with attendant precision losses.

**Active-participants bridge.**
The backend writes `active_participants.json` listing the participants
currently streaming. The viewer reads this file at start-up to know
which windows to open and which streams to bind. This file-based bridge
avoids LSL stream-name parsing edge cases and lets the viewer crash
and reopen without resyncing.

**Schema versioning on state files.**
Every JSON state file has a `_schema_version` field. Reading an older
schema triggers a migration; reading a newer one fails loudly. This
means an updated BiHome can be dropped over an old install without
losing the researcher's device registry.

---

## 7. Data quality safeguards (UX framing)

The following are technical features, but they exist for HTI reasons —
they reduce the rate of "I think the recording is bad, can I trust it?"
moments after a session.

| Feature | HTI consequence |
|---|---|
| NaN sentinels in gaps | Researcher sees a flat-line, not stretched time |
| EMA clock-offset alignment | Multi-device fusion stays valid without manual sync |
| `ACC_MAX_AGE_S = 1.5 s` | Stale ACC samples are dropped, not pushed as fresh |
| `MonotonicGuard` on timestamps | Timestamps cannot regress; downstream tools never see negative dt |
| LabRecorderCLI as the recorder | Peer-reviewed, well-tested LSL recording — not a bespoke writer |
| `.csv` export alongside `.xdf` | Researcher can sanity-check a recording in Excel before MATLAB |
| Per-stream sample counters | The log file says "PolarECG pushed 47820 samples in 240 s" — researcher can verify rate |

---

## 8. Failure modes and recovery

A research session cannot be paused like a debugger. Recovery has to
happen in seconds, and the researcher should not need to know what went
wrong to fix it.

| Failure | What the user sees | What they do |
|---|---|---|
| Polar not worn yet | "Polar 1: scanning…" persists | Put it on, the connection completes |
| Bluetooth radio off | "BLE scan failed" hint with reason | Toggle Bluetooth in Settings |
| EmotiBit on wrong WiFi | First scan times out, second scan starts | Switch the EmotiBit to the lab WiFi |
| Polar already paired in Polar Beat | "Unreachable" after 3 retries | Close Polar Beat, retry |
| Viewer subprocess crashes | Plots freeze | Click "Restart viewer" — backend keeps streaming |
| LabRecorderCLI not found | REC button greyed out with tooltip | Don't move `_internal/` |
| Smart App Control blocks .exe | App won't launch (web page on "More info") | Use `.bat` launcher (see DISTRIBUTION.md) |

All errors land in `acquisition.log` with a stack trace and timestamp.
The researcher can copy-paste the file to a maintainer without
reproducing the problem.

---

## 9. Distribution: bridging the deployment gap

Even excellent software fails the HTI test if installation is hard.

**Single-folder, no installer.** The bundled exe lives in
`BiHome Wearable/` alongside `_internal/`. Zip it, email it, double-click.
No admin rights, no registry edits, no MSI dialog asking about file
associations.

**Per-user app data.** `%APPDATA%/BiHome/` survives app updates.
Replacing `BiHome Wearable.exe` does not wipe the researcher's device
registry, recordings, or window layouts.

**Three failure escape hatches:**
1. Standard SmartScreen → "Run anyway" (documented).
2. Smart App Control → `.bat` launcher running from source (documented).
3. Future code-signed build → no warnings at all (planned).

This staircase lets the system reach increasingly conservative IT
environments without re-architecting.

---

## 10. What the system explicitly does *not* do

Equally important to HTI is what we chose **not** to build. Each item
below has been requested at some point and rejected for usability or
maintenance reasons.

- **No cloud sync.** Recordings stay on the lab machine. IRB and GDPR
  compliance is the institution's responsibility; we do not move data
  for them.
- **No automatic stimulus presentation.** BiHome is acquisition-only.
  Stimulus tools (PsychoPy, OpenSesame) send markers via LSL — we
  consume them, we do not control them.
- **No statistics, no analysis.** Output is XDF and CSV. The researcher
  uses their existing pipeline.
- **No participant-facing UI.** The participant sees no screen, no
  prompts, no calibration steps from BiHome. They are the subject, not
  the operator.
- **No machine learning, no inference, no "AI-detected stress."** Raw
  signals only. Inference is the researcher's domain.

---

## 11. Measured outcomes (qualitative, to date)

In informal piloting with the developer and one co-researcher, the
following improvements over the pre-BiHome workflow have been observed:

- **Setup time** from "lab PC on" to "first sample streaming" dropped
  from ~12 minutes (manual LSL scripts + LabRecorder + per-device apps)
  to **~2 minutes** with the wizard.
- **Setup error rate** (missed devices, wrong MACs, forgotten REC)
  dropped from "at least once per session" to "not observed in the last
  ~10 sessions."
- **Recovery time after a Polar disconnect** dropped from "restart the
  whole script" to "wait ~15 s for auto-reconnect" — typically without
  the researcher noticing.

These numbers are anecdotal and from a single pair of users; a proper
usability study with multiple labs is a planned follow-up.

---

## 12. Open HTI questions and planned future work

- **Tutorial / first-launch tour.** First-time users currently rely on
  DISTRIBUTION.md. An in-app guided overlay highlighting the wizard
  steps and the viewer's controls would lower the barrier further.
- **Macros / experiment templates.** A researcher running the same
  protocol weekly currently re-configures markers every time. A
  per-experiment template file (loaded from `%APPDATA%/BiHome/templates/`)
  would let them save and reuse setups.
- **Cross-platform.** macOS and Linux ports are technically feasible
  (the stack is pure Python + LSL + BrainFlow). The current Windows-only
  decision was made because all target labs run Windows; this may
  change.
- **Telemetry, opt-in.** No usage telemetry today. Adding opt-in error
  reporting would close the loop on "what is actually failing in the
  field."
- **Code-signed build.** Removes the SmartScreen / Smart App Control
  friction documented in §9 and §3 of DISTRIBUTION.md.
- **Accessibility.** No keyboard shortcuts for REC / marker buttons
  yet; no high-contrast theme; no screen-reader testing. Worth
  addressing as the user base grows.

---

## 13. Summary

BiHome Wearable is an exercise in making a multi-device LSL pipeline
*disappear* behind a wizard and a viewer. The HTI argument is that
psychology researchers should be able to focus on their participants,
their stimuli, and their hypotheses — not on debugging Bluetooth stacks
or aligning timestamps. Every design decision here was made to push
the cognitive load away from the moment-of-use and into either
(a) one-time setup or (b) post-hoc analysis where the researcher has
time and tools.

The system is far from finished, and the open questions in §12 will
shape the next iterations. But the foundational HTI thesis — that
*consolidation reduces error rate more than feature richness increases
it* — has held up in early use.
