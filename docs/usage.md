# Usage

Contents: [GUI](#gui-edu09-gui) | [MCP server](#mcp-server-edu09-mcp) | [Python library](#python-library) |
[Spectrum analyzer](#spectrum-analyzer) | [Calibration](#calibration) | [Probe tool](#the-probe-tool-edu09-probe)

Installation and troubleshooting: [install.md](install.md). Hardware tests and contributing:
[../CONTRIBUTING.md](../CONTRIBUTING.md). Wire protocol: [protocol.md](protocol.md).

## GUI (`edu09-gui`)

```
edu09-gui [--simulate SPEC] [--mcp-http] [--port N] [--no-run] [--record FILE] [-v]
```

`--simulate SPEC` runs on the simulator (no hardware, e.g. `"sine:1khz:2vpp"`), `--mcp-http`
starts the MCP server at launch (`--port`, default 8765), `--no-run` does not start acquiring,
`-v` enables debug logging.

`--record FILE` appends every USB report exchanged with the scope to `FILE` (JSONL, one line per
report with a monotonic timestamp; flushed immediately, appended across runs and reconnects). Use
it to capture problems for a bug report; it changes nothing else (the owner lock and calibration
file are used as without it).

On Linux the GUI checks first that `libxcb-cursor` is installed (Qt's X11 plugin aborts without
it) and prints install instructions instead of crashing; see [install.md](install.md). Set
`EDU09_SKIP_QT_CHECK=1` to skip the check if it misdetects the library.

### Screen

- **State indicator** (top of the Acquisition group): RUNNING (green), STOPPED (red), SINGLE and
  WAITING FOR TRIGGER (amber; waiting means armed but no trigger yet), BUSY (blue; calibration or
  auto-set in progress) and DISCONNECTED (grey; the driver keeps reconnecting).
- **Plot** (centre): the 8x10 screen with the trace in volts, time relative to the trigger point,
  a trigger-level marker (drag it to change the level) and a badge ("Trig'd", "Auto
  (untriggered)", "Free run"). Wheel and drag zoom and pan the time axis. Ctrl+wheel (Cmd on
  macOS) zooms the vertical axis around the pointer and Shift+wheel pans it; this is display
  only (V/div and position are unchanged), limited to 0.5 div up to the full 8-div screen, and
  the header shows "Y zoom xN". Changing V/div or position resets the vertical zoom.
- **Vertical (CH1)**: V/div (0.1 to 5), coupling AC/DC/GND, position (the ground marker, +-4 div).
- **Horizontal**: timebase 500 ms to 10 us per div. 50/20/10 us use equivalent-time sampling
  (ETS): they need a repetitive, triggered signal.
- **Trigger**: mode (Auto, Normal, Off), edge, level in volts.
- **Acquisition**: Run, Stop, Single, Auto-set.
- **Measurements and cursors**: Vpp, Vrms, mean, frequency, period, duty, rise/fall time, ... for the
  frame on screen. View > Despike removes isolated ETS outlier samples (ETS frames only).
- **Spectrum** (View > Spectrum): see [Spectrum analyzer](#spectrum-analyzer). In its panel the
  wheel zooms frequency, Ctrl+wheel zooms dB (turns off Auto Y) and a double-click resets the view.
- **Tools > MCP server (HTTP)**: serve MCP at `http://127.0.0.1:8765/mcp` so an AI agent shares
  the scope with the GUI (the owner label in the status bar shows the URL).
- **Tools > Calibrate...**: opens the calibration wizard (see [Calibration](#calibration)). The
  main plot stays visible behind it; it pauses briefly while a step measures.
- Closing the window while calibrating or auto-setting asks what to do: *Wait, then quit* (the
  indicator shows "QUIT PENDING"), *Quit anyway* (aborts the job: a calibration
  stops at its next device access and nothing more is measured or saved; the scope is stopped and
  released) or *Cancel*. Closing the
  GUI also aborts a calibration that an MCP client started through the GUI's MCP server.

### Keyboard shortcuts

| Shortcut | Action |
|---|---|
| F5 / F6 / F7 | Run / Stop / Single |
| Ctrl+E | File > Export CSV (current waveform with time and volts) |
| Ctrl+K | View > Cursors |
| Ctrl+F | View > Spectrum |
| Ctrl+0 | View > Reset zoom (both axes) |
| Wheel / Ctrl+wheel / Shift+wheel | zoom time / zoom volts (Cmd on macOS) / pan volts |

## MCP server (`edu09-mcp`)

```
edu09-mcp [--http] [--host H] [--port N] [--allow-remote]
          [--allowed-host NAME[:PORT]] [--no-bridge] [--simulate [SPEC]]
          [--allow-calibration] [--sim-unit ideal|unit1] [--export-dir DIR]
          [--record FILE] [--log-level DEBUG|INFO|WARNING|ERROR]
```

stdio by default (stdout is the protocol, logs go to stderr). `--http` serves streamable HTTP on
`127.0.0.1:8765/mcp`. Client configuration for Claude Code is in the [README](../README.md#mcp-with-claude-code).

- **Simulator.** `--simulate [SPEC]` with specs like `sine:1khz:2vpp` (default),
  `square:50hz:5vpp:duty=25%:offset=2.5v`, `dc:1.5v`, `noise:100mvrms`. `--sim-unit unit1` gives
  the simulated scope the gain errors and negative nonlinearity measured on a real unit, to try
  the calibration tools (default `ideal`).
- **Bridge.** When the GUI (or another `edu09-mcp --http`) owns the device and serves MCP, a plain
  stdio `edu09-mcp` forwards all tools and resources to it automatically, so an agent's stdio
  config works whether or not the GUI is open. Only loopback owner URLs are accepted. In bridge
  mode `--allow-calibration`, `--export-dir` and `--record` are ignored (the owner decides).
  `--no-bridge` makes it exit (code 2) instead. If the owner publishes no MCP URL, or the URL is
  not loopback, `edu09-mcp` prints who owns the scope and exits with code 2. If the owner quits
  mid-session (its `owner.json` is gone), tool calls return `OwnerGone` errors (the old URL is not
  reused); restart `edu09-mcp` to open the device directly. A forwarded tool call is cut off after
  330 s, and upstream JSON responses larger than 8 MiB are refused.
- **Remote use.** `--host` other than loopback needs `--allow-remote`; add the names clients use
  with `--allowed-host NAME[:PORT]` (repeatable, the port defaults to `--port`), e.g.
  `edu09-mcp --http --host 0.0.0.0 --allow-remote --allowed-host mypc.lan`. `--allowed-host` and
  `--allow-remote` only apply with `--http`; without it they are ignored with a warning on stderr.
  With a wildcard bind (`0.0.0.0` / `::`) the URL published for the bridge is
  `http://127.0.0.1:PORT/mcp` (or `http://[::1]:PORT/mcp`). There is **no authentication or TLS**:
  trusted networks only (see [SECURITY.md](../SECURITY.md)).
- **Debug recording.** `--record FILE` appends every USB report to a JSONL file (ignored in bridge
  mode).
- **Exports.** `export_csv` writes into `--export-dir` (default: `exports/` in the user data
  directory, see [install.md](install.md#files-and-locations)).

### Tools

| Tool | Parameters (defaults) | Purpose |
|---|---|---|
| `get_status` | | connection, firmware, state (`idle`, `running`, `armed`, `disconnected`), settings, last waveform id, calibration source, `calibrated` (false without a calibration file, plus `calibration_warning`), simulated flag, owner |
| `get_capabilities` | | allowed V/div, timebases (with sample rates, ETS flag), couplings, trigger modes and edges, position range, measurement names, limits |
| `configure` | `volts_per_div`, `coupling`, `timebase`, `trigger_mode`, `trigger_edge`, `trigger_level_v`, `position_div`, `trigger_auto_timeout_s` (all optional) | partial atomic settings update; returns the new settings and warnings for coercions |
| `acquire` | `timeout_s` (2.0; 0.1..30), `max_points` (200; 16..2048), `trigger_mode` | single shot (while running: the next new frame); returns a waveform id, stats (min, max, mean, Vpp, Vrms, frequency) and decimated points |
| `get_waveform` | `waveform_id` (`latest`), `start`, `stop`, `max_points` (1024), `decimation` (`minmax`/`stride`), `include_raw` | a stored waveform (`latest` or an id such as `w12`), optionally windowed/decimated |
| `measure` | `waveform_id`, `measurements`, `despike` | named measurements (`vpp`, `vmax`, `vmin`, `mean`, `vrms`, `ac_rms`, `frequency`, `period`, `duty`, `rise_time`, `fall_time`, `pos_width`, `neg_width`) on the full frame |
| `spectrum` | see [Spectrum analyzer](#spectrum-analyzer) | FFT amplitude spectrum of a stored waveform: fundamental, THD, peaks, dBV points |
| `export_csv` | `waveform_id`, `filename`, `overwrite` | write a stored waveform to a plain `*.csv` file in the export directory |
| `set_running` | `running` | start or stop continuous acquisition |
| `list_waveforms` | | the last 32 stored waveforms, newest first |
| `auto_set` | `timeout_s` (5.0; 0.5..60) | pick V/div, timebase and trigger level for an unknown signal (100 mV..30 V, > 50 Hz) |
| `calibrate` | `confirm`, `input_disconnected` | only with `--allow-calibration`; re-measures the offsets (stored gains are kept) and saves with a backup; needs `confirm=true` and `input_disconnected=true` |
| `calibration_plan` | `sources`, `mode`, `max_abs_v` (15), `trimmer` | only with `--allow-calibration`; plan the wizard for the human's sources (pure, no device access): setups, coverage per range, hints |
| `calibration_start` | as `calibration_plan` plus `uncalibrated` (`keep`) | start the wizard session (one at a time); returns `session_id` and the first step |
| `calibration_step` | | current step: instruction, `needs` (`input_disconnected`, `reading_v`, `done`, `none`), last result; in the trimmer's live phase each call returns one live sample |
| `calibration_submit` | `action`, `reading_v`, `input_disconnected` | `action` is `measure` (zero step: `input_disconnected=true`; other steps: `reading_v` = the multimeter reading), `retry`, `skip` or `done` (trimmer) |
| `calibration_finish` | `save`, `confirm` | `save=false`: fit preview; `save=true, confirm=true`: store it (backup made), then the verify steps follow |
| `calibration_abort` | | abort the session (nothing more is saved); returns the session log path |

The `calibrate` and `calibration_*` tools exist only with `--allow-calibration`. Readings must be
typed by the human from their multimeter: the agent must never invent or estimate them, and asks
the human to do every physical step. A session is aborted after 30 minutes without a tool call and
when the server stops. Responses are small (no raw samples, at most 8 KB). Numbers in payloads are
rounded to 4 significant digits; `minmax`-decimated points are not evenly spaced and carry sample
offsets `i` (time = `t0_s + i * sample_dt_s`), `stride` points carry `dt_s`.

Resources: `edu09://status`, `edu09://capabilities`, and per-waveform full-resolution JSON
`edu09://waveforms/{id}` and CSV `edu09://waveforms/{id}.csv`.

## Python library

```python
from edu09 import Scope, analysis

with Scope.open() as scope:  # first attached scope; takes the owner lock
    scope.configure(volts_per_div=0.5, timebase="200us", trigger_mode="normal", trigger_level_v=0.0)
    wf = scope.acquire(timeout_s=2.0)  # edu09.Waveform: 1024 samples
    print(wf.t()[:3], wf.volts[:3])  # time (s, relative to the trigger) and volts
    m = analysis.measure(wf.volts, wf.dt)  # {"vpp": ..., "frequency": ...}
```

Without hardware pass a simulator: `Scope.open(SimTransport(SignalSpec.parse("sine:1khz:2vpp"),
seed=None))` with `from edu09.transport.sim import SignalSpec, SimTransport`. `Scope` is thread-safe
(one worker thread does all I/O), also offers `run()`, `stop()`, `subscribe(callback)`,
`acquire_async()` and `set_running()`, and raises the errors in `edu09.errors` (`DeviceNotFound`,
`DeviceBusy`, `DeviceAccessError`, ...). The calibration engine is `edu09.calwizard`; the design is in
[specs/architecture.md](specs/architecture.md).

## Spectrum analyzer

FFT of the acquired 1024-sample frame: 513 bins from 0 to fs/2, spaced fs/1024 (152.6 Hz at
200 us/div, 0 to 78 kHz). GUI: View > Spectrum (Ctrl+F; window, averaging over 4/8/16 frames,
peak hold, log frequency, auto Y, despike). MCP: the `spectrum` tool. Library:
`edu09.analysis.spectrum(samples, dt)`, `spectrum_average(frames, dt, mode="power"|"peak_hold")`
and `spectrum_waveform(wf)`.

- **Units.** Single-sided RMS amplitude per bin in Vrms and dBV (0 dBV = 1 Vrms), corrected for
  the window's coherent gain: a sine of amplitude A reads A/sqrt(2) Vrms. The mean (DC) is
  removed first and reported separately as `dc_v`.
- **Windows.** `hann` (default), `hamming`, `blackman`, `flattop`, `rect`. Peak amplitudes are
  corrected for scalloping (within 0.1 % with `flattop`, 0.2 % `blackman`, 0.7 % `hann`, 0.9 %
  `hamming`, up to 16 % `rect`); raw bin values can be up to 15 % low with `hann` when a tone falls
  between two bins. Use `flattop` for amplitudes, `hann`/`blackman` to separate close tones.
- **Peaks and THD.** Peaks are local maxima within 60 dB of the strongest bin, with frequency
  interpolated between bins. The fundamental is the strongest peak; THD uses harmonics 2..10 below
  fs/2 (no THD if none is below fs/2 or the fundamental is below bin 6).
- **Aliasing.** The EDU09 has no anti-alias filter and about 200 kHz input bandwidth, so on every
  realtime timebase (fs/2 at most 78 kHz) components above fs/2 fold back into the spectrum: a
  30 kHz square at 200 us/div shows its 3rd harmonic at 156.25 - 90 = 66 kHz, the 4th at 36 kHz,
  the 5th at 6 kHz. The tool warns about this. Choose a timebase whose fs/2 is well above the
  frequencies of interest, or treat peaks near the harmonics' alias positions with care.
- **ETS (50/20/10 us/div).** Equivalent-time frames (fs = 625 kHz / 1.5625 MHz) are only valid
  for a stable repetitive signal; the spectrum is labelled accordingly. `despike` (ETS only)
  removes isolated outlier samples first.
- **Other limits.** 8-bit ADC: the noise floor is about 50 dB below full scale. A clipped frame
  adds false harmonics (warning). "too few periods" warns when the fundamental is below 3 bins;
  use a slower timebase. 0.1 ms and 10 us/div are display zooms of 0.2 ms and 20 us/div: the
  spectrum uses the real sample interval.

MCP `spectrum(waveform_id="latest", window="hann", max_points=512, peaks=8, db_floor=-80,
despike=false)` returns `fs_hz`, `resolution_hz`, `n`, `dc_v`, `fundamental` `{freq_hz, vrms,
dbv}` (or null), `thd_pct`, `peaks`, `points` and `warnings`. `points.dbv` holds the dBV of each
bin, floored at `db_floor` (-200..0); bin `k` is at `f0_hz + k * df_hz`. When there are more bins
than `max_points` (16..1025) the bins are split into `max_points` near-equal buckets and the
strongest bin of each is kept (so peaks survive), and `points.i` gives each point's bin index.
`peaks` is 0..32.

Example (hardware frame of a 555 square wave, ~0..4.3 V, ~58 % duty, 200 us/div, `flattop`):
fundamental 29.99 kHz, 1.65 Vrms (4.4 dBV), 2nd harmonic 60.0 kHz at -3.7 dBV, aliased
harmonics at 36.2, 66.2, 23.9 and 6.2 kHz, THD 40 % (only the 2nd harmonic is below fs/2).

## Calibration

Every unit differs a little: the offset DACs (where 0 V is), the input amplifier gain of each
V/div range, and how much large negative signals are stretched. The **calibration wizard**
measures all of it for your unit with nothing more than a multimeter and one or two DC sources
(batteries, a USB 5 V, a bench supply), then saves a per-unit `calibration.json` (the previous
file is backed up as `calibration.<stamp>.bak.json`). Without a calibration file the driver uses
typical offsets with no gain correction and reports the scope as uncalibrated: an orange
"Uncalibrated" in the GUI status bar, `calibrated: false` and a `calibration_warning` in MCP
`get_status`, and a warning in `acquire` results.

Front ends (same engine): **GUI** Tools > Calibrate..., **CLI** `edu09-calibrate`, and **MCP**
`calibration_*` tools (with `edu09-mcp --allow-calibration`). Close the GUI and any MCP server
before using the CLI (one program owns the scope).

Input limits: at most **30 V** (AC + DC) at CH1; input impedance **100 kOhm**. The wizard caps the
voltages it asks for at 15 V by default (`--max-volts` / "Max voltage", at most 30).

### Recommended order

1. **RV1 to its middle position** (only the first time, or if it was turned before). RV1 is the
   trimmer between IC1 (CD74HCT4052) and IC2 on the board; the WSEDU09 manual calls it the "fine
   tuning of the input amplifier circuit". It sets the gain of **all** ranges.
2. **Zero** (input disconnected): probe tip and ground clip free. Measures the offsets and the
   0 V level at every range/position the plan uses.
3. **Trim RV1** (optional "Trimmer step", 0.5 V/div DC): connect a 1.5 V battery, type its
   multimeter reading, then turn RV1 slowly with a small (insulated) screwdriver until the live
   deviation is ~0. This is the vendor's "advanced calibration". Because RV1 changes the gain of
   all ranges, the gain steps come after it.
4. **Per-range gain steps**: connect each planned source, type the multimeter reading at the
   probe tip, measure. Reversible (floating) sources are also measured with swapped leads for the
   negative side and its nonlinearity.
5. **Preview and save**, then **verify**: one more reading at shifted positions through the
   saved calibration. If it fails, revert to the previous calibration or run the wizard again.

### Sources

Describe what you have; the wizard plans the steps and reports per range what is covered (`ok`,
`fallback`, `missing`) and which voltages would cover the rest. A missing range keeps its
previous gain (none on a unit without a calibration file; or, with "uncovered ranges: typical",
the values measured on one unit, `tables.TYPICAL_RANGE_GAIN`).

- **Fixed**: a voltage with a tolerance (e.g. AA 1.5 V +-10 %, USB 5 V).
- **Adjustable**: a supply with a range (e.g. 0..15 V); the wizard tells you what to set.
- **Reversible**: you may swap its leads. **Only reverse floating sources** (batteries, isolated
  bench supplies). Never reverse a source that shares ground with this PC (e.g. USB 5 V from the
  same computer): the probe ground clip is the PC ground.
- No suitable voltage? Divide a larger source with two resistors (well below the 100 kOhm input
  impedance, e.g. 1-10 kOhm); only the multimeter reading at the probe tip matters.

The sources and options are remembered in `calibration_sources.json` (config dir; no secrets).

**The 5 V/div range** needs references of 20-30 V (4-6 div of deflection at 5 V/div), above the
default 15 V cap. With a floating adjustable supply raise the cap and restrict the run to that
range, for example
`edu09-calibrate --ranges 5 --max-volts 25 --adjustable 0..25,rev,label=bench --uncalibrated keep`
(positive and negative gain; the negative nonlinearity beyond ~5 div would need about -28..-32 V,
close to the 30 V input limit, so it stays partial). Note: the zero step of a restricted run still
refits the global offsets, which can move the ground of the other ranges by up to 1 count.

### GUI

Tools > Calibrate... opens the wizard: **Sources** (table, mode quick/standard/full, max voltage,
trimmer step, uncovered ranges), **Plan** (steps, coverage grid, hints, estimated time),
**Measure** (one page per step: instruction, multimeter reading, Measure / Retry / Skip; the zero
step needs the "The input is disconnected" checkbox; the trimmer step shows a large live DC mean,
the reference, the deviation in mV and %, best so far and the trend, until "Done adjusting"),
**Result** (gains per range, error before/after, warnings; "Save calibration" or "Close without
saving") and **Verify** (with "Revert to previous calibration"). Measurements run in the
background; Cancel asks before discarding measured points and nothing is stored before Save.

### CLI

```bash
edu09-calibrate --plan-only --fixed 1.5@10%,rev,label=AA --fixed 5,label=USB      # no device
edu09-calibrate --adjustable 0..15,rev,label=bench --trimmer                       # full run
edu09-calibrate --simulate --sim-unit unit1 --yes --calibration /tmp/cal.json       # demo on the simulator
```

`--fixed VOLTS[@TOL%][,rev][,label=TEXT]`, `--adjustable MIN..MAX[,rev][,label=TEXT]`, `--mode
quick|standard|full`, `--max-volts`, `--ranges` (e.g. `0.1,0.2,0.5`), `--no-zero`, `--no-verify`,
`--uncalibrated keep|typical|unity`, `--trimmer [first|after_zero]`, `--trimmer-vdiv`,
`--trimmer-reference source|open`, `--trimmer-source LABEL`, `--plan-only` (with `--json` for
machine-readable output), `--calibration PATH`, `--save-sources`, and for tests `--simulate`,
`--sim-unit`, `--sim-clock real|virtual`. Per step it asks for the multimeter reading (`r` retry,
`s` skip, `q` quit: nothing is saved). `--yes` only works with `--simulate` (real readings come
from a human).

### MCP

Start `edu09-mcp --allow-calibration` and let the agent drive `calibration_plan` ->
`calibration_start` -> (`calibration_step` -> ask the human -> `calibration_submit`)... ->
`calibration_finish(save=true, confirm=true)` -> verify steps. The agent must ask you for every
physical action and every multimeter reading. The older one-shot `calibrate` tool (offsets only)
still exists.

### What is stored

`calibration.json` in the user config directory ([locations](install.md#files-and-locations)):

- offsets: `base_ofs`, `range_corr`, `gnd_fit`;
- `range_gain`: positive-side gain `g = V_true / V_read` per range (string keys in V/div; missing
  ranges use 1.0; accepted `0.8 < g < 1.25`);
- `range_gain_neg`: negative-side gain (missing: same as `range_gain`);
- `neg_nonlinearity`: per range, knots `[r, e]` of the extra stretch of large negative deflections
  (in divisions; applied by the driver);
- `meta`: provenance (tool, date, residuals; not part of the calibration id).

Files with only offsets and `range_gain` are written as version 1 (older builds read them);
anything else as version 2. On the development unit: base offset `0x86` (134), range corrections
`{1 V: -1, 0.1 V: -1}`, k ~ 1.03, b ~ -9.8; gains between 0.966 and 1.013 depending on how RV1 was
set (the one-unit table used by `--uncalibrated typical` is 0.974-0.996); 0.2 and 0.5 V/div stretch ~6 % at 6
div below ground. Plausible results: base offset within 0x5A..0xA6, k about 1.0. A zero-step
failure starting with "CALIBRATION FAILED" usually means a voltage is connected. Session logs
(plan, every point, fit, verify, trimmer samples) go to `calibration-sessions/<stamp>.json` in the
user data directory (the newest 20 are kept); keep them when reporting a problem.

### Low-level tools

`edu09-gain --vref 4.95 --vdiv 1 [--position DIV] [--frames N] [--save] [--calibration PATH]`
measures one range against one positive DC reference (GUI and MCP server closed); the MCP
`calibrate` tool / `Scope.calibrate` re-measure only the offsets with the input disconnected (they
keep the stored gains). From Python: `edu09.device.calibration.gain_from_reference(scope, v_ref,
volts_per_div)`, and `edu09.calwizard` for the wizard engine (`plan_calibration`,
`CalibrationSession`).

## The probe tool (`edu09-probe`)

Low-level protocol explorer that logs every report as JSONL under `captures/` (git-ignored; set
`--captures-dir`, `--capture FILE` or `--no-capture`). It uses only the protocol and transport
layers, never the driver (and does not take the owner lock, so do not run it while the GUI or MCP
server is acquiring). Subcommands: `list`, `version`, `listen`, `config HEX...`, `acquire`,
`send HEX...`, `replay FILE`. Run `edu09-probe SUBCOMMAND --help` for the options.

Safety levels (by opcode class):

| Level | Opcodes | Needs |
|---|---|---|
| read | `0x0F` version | nothing |
| acquire | `0x08 0x09 0x0A 0x0B 0x0D 0x0E` | `--allow-acquire` |
| unknown | `0x0C`, `0x14`, any other opcode, malformed config | `--allow-unknown` and an interactive typed confirmation |

Ctrl-C while acquiring sends the stop command (`0x08`). Stop the GUI before using anything but
`list`. To contribute a capture see [CONTRIBUTING.md](../CONTRIBUTING.md#contributing-captures-and-calibration-data).
