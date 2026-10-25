# Usage

## GUI (`edu09-gui`)

Options: `--simulate SPEC` (no hardware), `--mcp-http` (start the MCP server at launch),
`--port N`, `--no-run`, `--record FILE`, `-v`.

`--record FILE` appends every USB report exchanged with the scope to `FILE` (JSONL, one line per
report with a monotonic timestamp; flushed immediately, appended across runs and reconnects). Use
it to capture problems for a bug report; it changes nothing else (owner lock, calibration file
are used as without it).

Quitting while the calibration wizard is open asks whether to wait or quit anyway; "quit anyway"
aborts the wizard's session at its next device access (nothing more is measured or saved, the
scope is stopped). Closing the GUI also aborts a calibration that an MCP client started through
the GUI's MCP server.

On Linux the GUI checks first that `libxcb-cursor` is installed (Qt's X11 plugin aborts without
it) and prints install instructions instead of crashing: `libxcb-cursor0` on Debian/Ubuntu and
openSUSE, `xcb-util-cursor` on Fedora and Arch. Set `EDU09_SKIP_QT_CHECK=1` to skip the check if it
misdetects the library.

- **State indicator** (top of the Acquisition group): RUNNING (green), STOPPED (red), SINGLE and
  WAITING FOR TRIGGER (amber; waiting means armed but no trigger yet), BUSY (blue; calibration or
  auto-set in progress) and DISCONNECTED (grey; the driver keeps reconnecting).
- **Closing the window while calibrating or auto-setting** asks what to do: *Wait, then quit*
  (the indicator shows "QUIT PENDING"), *Quit anyway* (aborts the job; the scope is stopped and
  released) or *Cancel*.

- **Plot** (centre): the 8x10 screen with the trace in volts, time relative to the trigger point,
  a trigger-level marker and a badge ("Trig'd", "Auto (untriggered)", "Free run"). Mouse wheel and
  drag zoom/pan the time axis; Ctrl+wheel (Cmd on macOS) zooms the vertical axis around the
  pointer (display only: V/div and position are unchanged; limits 0.5 div up to the full 8-div
  screen; the header shows "Y zoom xN"); Shift+wheel pans it. Changing V/div or position resets
  the vertical zoom. View > Reset zoom (Ctrl+0) resets both axes. In the spectrum panel the wheel
  zooms frequency, Ctrl+wheel zooms dB (turns off Auto Y) and a double-click resets the view.
- **Vertical (CH1)**: V/div (0.1 to 5), coupling AC/DC/GND, position (the ground marker, +-4 div).
- **Horizontal**: timebase 500 ms to 10 us per div. 50/20/10 us use equivalent-time sampling:
  they need a repetitive, triggered signal.
- **Trigger**: mode (Auto, Normal, Off), edge, level in volts, auto timeout.
- **Acquisition**: Run (F5), Stop (F6), Single (F7), Auto-set.
- **Measurements and cursors** (View > Cursors, Ctrl+K): Vpp, Vrms, mean, frequency, period,
  duty, rise/fall time, ... for the frame on screen. View > Despike removes ETS outlier samples.
- **File > Export CSV** (Ctrl+E): the current waveform with time and volts.
- **Tools > MCP server (HTTP)**: serve MCP at `http://127.0.0.1:8765/mcp` so an AI agent shares
  the scope with the GUI (the owner label in the status bar shows the URL).
- **Tools > Calibrate...**: opens the calibration wizard (see [Calibration](#calibration)). The
  main plot stays visible behind it; it pauses briefly while a step measures.

## MCP server (`edu09-mcp`)

```
edu09-mcp [--http] [--host H] [--port N] [--allow-remote]
          [--allowed-host NAME[:PORT]] [--no-bridge] [--simulate [SPEC]]
          [--allow-calibration] [--sim-unit ideal|unit1] [--export-dir DIR]
          [--record FILE] [--log-level L]
```

stdio by default (stdout is the protocol, logs go to stderr). `--http` serves streamable HTTP on
`127.0.0.1:8765/mcp`. Simulator specs: `sine:1khz:2vpp`, `square:50hz:5vpp:duty=25%:offset=2.5v`,
`dc:1.5v`, `noise:100mvrms`. `--sim-unit unit1` (with `--simulate`) gives the simulated scope the
gain errors and negative nonlinearity measured on a real unit, to try the calibration wizard tools
(default `ideal`). `--record FILE` appends every USB report to a JSONL file (debugging;
ignored in bridge mode). `--allowed-host` and `--allow-remote` only apply with `--http`; without
it they are ignored with a warning on stderr. 
**Bridge.** When the GUI (or another `edu09-mcp --http`) owns the device and serves MCP, a plain
stdio `edu09-mcp` forwards all tools and resources to it automatically, so an agent's stdio config
works whether or not the GUI is open. Only loopback owner URLs are accepted. In bridge mode
`--allow-calibration`, `--export-dir` and `--record` are ignored (the owner decides). `--no-bridge` makes it exit
instead. If the owner publishes no MCP URL, or the URL is not loopback, `edu09-mcp` prints who owns
the scope and exits with code 2. If the owner quits mid-session (its `owner.json` is gone), tool
calls return `OwnerGone` errors (its old URL is not reused); restart `edu09-mcp` to open the device
directly. A forwarded tool call is cut off after 330 s, and upstream JSON responses larger than
8 MiB are refused.

**Remote use.** `--host` other than loopback needs `--allow-remote`; add the names clients use
with `--allowed-host NAME[:PORT]` (repeatable, port defaults to `--port`), e.g.
`edu09-mcp --http --host 0.0.0.0 --allow-remote --allowed-host mypc.lan`. With a wildcard bind
(`0.0.0.0` / `::`) the URL published for the bridge is `http://127.0.0.1:PORT/mcp` (or
`http://[::1]:PORT/mcp`). There is **no
authentication or TLS**: trusted networks only.

Tools:

| Tool | Purpose |
|---|---|
| `get_status` | connection, firmware, state, settings, last waveform id, calibration source, owner |
| `get_capabilities` | allowed V/div, timebases (with sample rates, ETS flag), couplings, trigger modes, limits |
| `configure` | partial atomic settings update (`volts_per_div`, `coupling`, `position_div`, `timebase`, `trigger_mode`, `trigger_edge`, `trigger_level_v`, `trigger_auto_timeout_s`); returns warnings for coercions |
| `acquire` | single shot; returns a waveform id, stats (min, max, mean, Vpp, Vrms, frequency) and decimated points |
| `get_waveform` | a stored waveform (`latest` or an id such as `w12`), optionally windowed/decimated |
| `measure` | named measurements on a stored waveform |
| `spectrum` | FFT amplitude spectrum of a stored waveform: fundamental, THD, peaks, dBV points (see [Spectrum analyzer](#spectrum-analyzer)) |
| `export_csv` | write a stored waveform to a plain `*.csv` file in the export directory |
| `set_running` | start or stop continuous acquisition |
| `list_waveforms` | the last 32 stored waveforms |
| `auto_set` | pick V/div, timebase and trigger level for an unknown signal (100 mV..30 V, > 50 Hz) |
| `calibrate` | only with `--allow-calibration`; offset-only calibration; needs `confirm=true` and `input_disconnected=true` |
| `calibration_plan` | only with `--allow-calibration`; plan the wizard for the human's sources (pure, no device access): setups, coverage per range, hints |
| `calibration_start` | start the wizard session (one at a time); returns `session_id` and the first step |
| `calibration_step` | current step: instruction, `needs` (`input_disconnected`, `reading_v`, `done`, `none`), last result; in the trimmer's live phase each call returns one live sample |
| `calibration_submit` | `action` `measure` (with `input_disconnected=true` for the zero step or `reading_v` = the multimeter reading), `retry`, `skip`, `done` (trimmer) |
| `calibration_finish` | `save=false`: fit preview; `save=true, confirm=true`: store it (backup made), then the verify steps follow |
| `calibration_abort` | abort the session (nothing more is saved); returns the session log path |

The `calibration_*` wizard tools exist only with `--allow-calibration`. Readings must be typed
by the human from their multimeter: the agent must never invent or estimate them, and asks the
human to do every physical step. A session is aborted after 30 minutes without a tool call and
when the server stops. Responses are small (no raw samples, at most 8 KB).

Resources: `edu09://status`, `edu09://capabilities`, and per-waveform JSON/CSV
(`edu09://waveform/{id}` and its CSV variant).

## Spectrum analyzer

FFT of the acquired 1024-sample frame: 513 bins from 0 to fs/2, spaced fs/1024 (152.6 Hz at
200 us/div, 0 to 78 kHz). GUI: View > Spectrum (Ctrl+F). MCP: the `spectrum` tool. Library:
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
  fs/2 (none below fs/2: no THD).
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
bin, floored at `db_floor`; bin `k` is at `f0_hz + k * df_hz`. When there are more bins than
`max_points` (16..1025) the bins are split into `max_points` near-equal buckets and the strongest
bin of each is kept (so peaks survive), and `points.i` gives each point's bin index.

Example (H1 hardware frame, 555 square ~0..4.3 V, ~58 % duty, 200 us/div, `flattop`):
fundamental 29.99 kHz, 1.65 Vrms (4.4 dBV), 2nd harmonic 60.0 kHz at -3.7 dBV, aliased
harmonics at 36.2, 66.2, 23.9 and 6.2 kHz, THD 40 % (only the 2nd harmonic is below fs/2).

## The probe tool (`edu09-probe`)

Low-level protocol explorer that logs every report as JSONL under `captures/` (git-ignored).
It uses only the protocol and transport layers, never the driver (and does not take the owner
lock, so do not run it while the GUI or MCP server is acquiring). Subcommands: `list`, `version`,
`listen`, `config HEX...`, `acquire`, `send HEX`, `replay FILE`.

Safety levels (by opcode class):

| Level | Opcodes | Needs |
|---|---|---|
| read | `0x0F` version | nothing |
| acquire | `0x08 0x09 0x0A 0x0B 0x0D 0x0E` | `--allow-acquire` |
| unknown | `0x0C`, `0x14`, any other opcode, malformed config | `--allow-unknown` and an interactive typed confirmation |

Ctrl-C while acquiring sends the stop command (`0x08`). Stop the GUI before using anything but `list`.

## Calibration

Every EDU09 differs a little: the offset DACs (where 0 V is), the input amplifier gain of each
V/div range, and how much large negative signals are stretched. The **calibration wizard**
measures all of it for your unit with nothing more than a multimeter and one or two DC sources
(batteries, a USB 5 V, a bench supply), then saves a per-unit `calibration.json` (the previous
file is backed up as `calibration.<stamp>.bak.json`).

Front ends (same engine): **GUI** Tools > Calibrate..., **CLI** `edu09-calibrate`, and **MCP**
`calibration_*` tools (with `edu09-mcp --allow-calibration`).

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
previous gain (or, with "uncovered ranges: typical", the values measured on one unit).

- **Fixed**: a voltage with a tolerance (e.g. AA 1.5 V +-10 %, USB 5 V).
- **Adjustable**: a supply with a range (e.g. 0..15 V); the wizard tells you what to set.
- **Reversible**: you may swap its leads. **Only reverse floating sources** (batteries, isolated
  bench supplies). Never reverse a source that shares ground with this PC (e.g. USB 5 V from the
  same computer): the probe ground clip is the PC ground.
- No suitable voltage? Divide a larger source with two resistors (well below the 100 kOhm input
  impedance, e.g. 1-10 kOhm); only the multimeter reading at the probe tip matters.

The sources and options are remembered in `calibration_sources.json` (config dir; no secrets).

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
quick|standard|full`, `--max-volts`, `--ranges`, `--no-zero`, `--no-verify`, `--uncalibrated
keep|typical|unity`, `--trimmer [first|after_zero]`, `--trimmer-vdiv`, `--trimmer-reference
source|open`, `--save-sources`. Per step it asks for the multimeter reading (`r` retry, `s` skip,
`q` quit: nothing is saved). `--yes` only works with `--simulate` (real readings come from a
human). Close the GUI and any MCP server first (one program owns the scope).

### MCP

Start `edu09-mcp --allow-calibration` and let the agent drive `calibration_plan` ->
`calibration_start` -> (`calibration_step` -> ask the human -> `calibration_submit`)... ->
`calibration_finish(save=true, confirm=true)` -> verify steps. The agent must ask you for every
physical action and every multimeter reading. The older one-shot `calibrate` tool (offsets only)
still exists.

### What is stored

`calibration.json` in the user config directory (`~/.config/edu09/calibration.json` on Linux):

- offsets: `base_ofs`, `range_corr`, `gnd_fit` (as before);
- `range_gain`: positive-side gain `g = V_true / V_read` per range (string keys in V/div; missing
  ranges use 1.0; accepted `0.8 < g < 1.25`);
- `range_gain_neg`: negative-side gain (missing: same as `range_gain`);
- `neg_nonlinearity`: per range, knots `[r, e]` of the extra stretch of large negative deflections
  (in divisions; applied by the driver);
- `meta`: provenance (tool, date, residuals; not part of the calibration id).

Files with only offsets and `range_gain` are written as version 1 (older builds read them);
anything else as version 2. Values on the development unit: base offset `0x86` (134), range
corrections `{1 V: -1, 0.1 V: -1}`, k ~ 1.03, b ~ -9.8, gains 0.975-0.996, 0.2 and 0.5 V/div
stretch ~6 % at 6 div below ground. Plausible results: base offset within 0x5A..0xA6, k about 1.0.
A zero-step failure starting with "CALIBRATION FAILED" usually means a voltage is connected.
Session logs (plan, every point, fit, verify, trimmer samples) go to
`calibration-sessions/<stamp>.json` in the user data directory (the newest 20 are kept); keep
them when reporting a problem.

### Low-level tools

`edu09-gain --vref 4.95 --vdiv 1 [--save]` still measures one range against one positive DC
reference (GUI and MCP server closed), and the MCP `calibrate` tool / `Scope.calibrate` re-measure
only the offsets with the input disconnected (they keep the stored gains). From Python:
`edu09.device.calibration.gain_from_reference(scope, v_ref, volts_per_div)`, and
`edu09.calwizard` for the wizard engine (`plan_calibration`, `CalibrationSession`).

## Hardware tests

`tests/hw` talks to a real scope and is skipped unless you pass `--hardware` (or `EDU09_HW=1`).
Close the GUI and any MCP server first: tests skip with a message if the device is owned by
another process.

```bash
pytest --hardware -m hardware tests/hw -v                  # signal-free tests only
EDU09_HW_SIGNAL="square:30khz:0..4.16v" pytest --hardware -m hardware tests/hw -v
EDU09_HW_INPUT_OPEN=1 pytest --hardware -m hardware tests/hw -v
EDU09_HW_CALIBRATE=1 pytest --hardware -m hardware tests/hw/test_hw_calibration.py -v
```

| Flag | Meaning |
|---|---|
| (none) | discovery, version `1.01`, open/close, owner lock, free-run frames at 100 us..20 ms timebases, AUTO fallback and NORMAL timeout with an unreachable level, >= 20 fps for 2 s, stop then acquire |
| `EDU09_HW_SIGNAL=KIND:FREQ:LO..HI` | a known signal on CH1, DC coupled, e.g. `square:30khz:0..4.16v` (FREQ in hz/khz/mhz, levels in volts at the input). Enables NORMAL rising/falling trigger position (index `units.trigger_index(tb)` +-2), frequency +-3 % (realtime and ETS 50/20/10 us, where the period fits), loose Vpp check, unreachable-level behaviour, NORMAL-trigger fps |
| `EDU09_HW_INPUT_OPEN=1` | CH1 disconnected and a saved calibration: 0 V within +-0.1 div for all ranges at positions -3/0/+3, first frame after a position change is clean |
| `EDU09_HW_CALIBRATE=1` | CH1 disconnected: calibration dry run (`save=False`, previous calibration restored), compared with the saved one |

Free-run tests never touch the calibration file. Set the flags only when the physical setup
matches; the tests cannot detect it.
