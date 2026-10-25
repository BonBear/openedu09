# Changelog

All notable changes are documented here. Format: [Keep a Changelog](https://keepachangelog.com/).

## [0.1.0] - 2026-10-25

First public version. Verified on a real Whadda WSEDU09 / Velleman EDU09 (firmware 1.01) on Linux
and Windows.

### Added

**Driver and tools**
- Driver library: HID transport (hidraw on Linux, hidapi elsewhere), protocol codecs, thread-safe
  `edu09.Scope` with one acquisition worker, automatic reconnect, and single-process device
  ownership through a file lock (a second program gets a "device busy" error naming the owner).
- 15 timebases (500 ms/div to 10 us/div; 50/20/10 us use equivalent-time sampling), six ranges
  (0.1 to 5 V/div), AC/DC/GND coupling, AUTO/NORMAL/off trigger with rising or falling edge, single
  shot and continuous run.
- Auto-set (V/div, timebase and trigger level for an unknown signal).
- Simulator for hardware-free use and tests (`--simulate`), plus recording of every USB report to
  JSONL (`--record`) and replay.
- `edu09-probe`: diagnostic tool with opcode safety gates and JSONL capture.
- Protocol documentation from reverse engineering, with the hardware verification results
  (`docs/protocol.md`).

**GUI** (`edu09-gui`)
- Live trace, trigger marker, cursors, measurements, CSV export, ETS despike option, acquisition
  state indicator, optional in-process MCP server.
- Ctrl+wheel vertical zoom and Shift+wheel pan on the time plot (display only; reset by V/div and
  position changes and Ctrl+0).
- Spectrum dock (View > Spectrum, Ctrl+F) with window choice, averaging, peak hold, log frequency,
  peak labels, fundamental/THD readout, crosshair, Ctrl+wheel dB zoom and double-click reset.
- Calibration wizard (Tools > Calibrate), confirmation when closing during calibration or auto-set,
  and a `libxcb-cursor` pre-flight message on Linux (`EDU09_SKIP_QT_CHECK=1` to skip).

**MCP server** (`edu09-mcp`)
- stdio and streamable HTTP (loopback by default); tools `get_status`, `get_capabilities`,
  `configure`, `acquire`, `get_waveform`, `measure`, `spectrum`, `export_csv`, `set_running`,
  `list_waveforms`, `auto_set`; resources for status, capabilities and stored waveforms.
- Bridge: a stdio `edu09-mcp` forwards to a GUI (or other process) that owns the device and serves
  MCP; loopback owner URLs only, `--no-bridge` to disable, exit code 2 with a clear message when
  the owner offers no MCP URL.
- Remote hardening: a non-loopback `--host` requires `--allow-remote`; `--allowed-host NAME[:PORT]`
  for extra Host headers; DNS-rebinding protection; still no authentication.
- Calibration tools only with `--allow-calibration` (`calibrate`, `calibration_plan`,
  `calibration_start`, `calibration_step`, `calibration_submit`, `calibration_finish`,
  `calibration_abort`); readings must come from the human, saving needs `confirm=true`.
  `--sim-unit unit1` simulates a unit with real gain errors for trying them.

**Calibration**
- Offset calibration (base offset, three-point ground fit, range corrections) with an offset-DAC
  settle wait (~20 ms) before arming; `Calibration.typical()` (offsets measured on one unit, no gain
  correction) is the fallback on hardware without a calibration file, which is then reported as
  uncalibrated (orange "Uncalibrated" in the GUI status bar, `calibrated: false` and
  `calibration_warning` in MCP `get_status`, a warning in `acquire` and in the log).
- Per-range gain correction, calibration file version 2 (separate negative-side gain and a
  piecewise negative-nonlinearity correction; version 1 files still load), `edu09-gain` for a single
  range.
- Calibration wizard for any unit: plans the steps from the DC sources you have, optional RV1
  trimmer adjustment with a live readout, per-range positive and negative gain, fit, save with
  backup, verify and revert. Front ends: GUI, `edu09-calibrate` and the MCP tools. The 5 V/div range
  is calibrated with `--ranges 5 --max-volts 25`.

**Spectrum analyzer**
- `edu09.analysis.spectrum` / `spectrum_average` / `spectrum_waveform`: FFT of the 1024-sample
  frame in Vrms/dBV with coherent-gain correction, hann/hamming/blackman/flattop/rect windows,
  scalloping-corrected peaks, fundamental, THD, power averaging and peak hold, and warnings for
  aliasing on realtime timebases, clipped and ETS frames. Also in the GUI and as the MCP `spectrum`
  tool.

**Tests, packaging, docs**
- Opt-in hardware test suite (`pytest --hardware tests/hw`; `EDU09_HW_SIGNAL`,
  `EDU09_HW_INPUT_OPEN`, `EDU09_HW_CALIBRATE`). It refuses `EDU09_HW_SIGNAL` together with
  `EDU09_HW_INPUT_OPEN` and fails once with a clear message when CH1 shows no signal.
- udev rule and installer script for Linux; CI on Linux and Windows.
- Install, usage, protocol and design documents, contributing and security guides.

### Changed
- On hardware the offset model uses the measured DAC slope (about 1.03 counts per step) instead of the
  vendor demo-mode slope of 2; the vertical gain is corrected per range and side of ground.
- The trigger position index is per timebase mode (255 realtime, 248/253/253 ETS) instead of the
  258 given in the vendor documentation; for triggered ETS frames the worker detects the edge in each frame (the position
  varies by a few samples between sessions).
- Live levels (calibration) come from free-run frames; the vendor's `0x0D` "live" command returns no
  samples on firmware 1.01 and is never sent by the driver.

### Fixed
- Hang in the live-level read caused by `0x0D` starting an endless status stream; every wait loop in
  the worker is now bounded by a deadline and independently by a report count.
- First frame after a position or range change was off by up to ~8 counts at its start (offset DAC
  settling).
- The trailing report after each read-out and a variable number of leading status reports (up to 64
  buffered on Windows) are drained or skipped.
- Loopback checks no longer depend on the Python patch release (IPv4-mapped IPv6 addresses are never
  treated as loopback).
- Calibration wizard trimmer readout measures from the measured ground map (the model ground was
  about one count off, which skewed the first gain).

### Known limitations
- macOS is untested; Windows needs Python 3.12 or 3.13 (no `hidapi` wheels for 3.15 yet).
  Other firmware versions and other scopes are untested; CH1 only.
- Without calibration an individual unit reads 0.4-2.6 % high and large negative deflections are
  stretched; the wizard corrects both (verified on one unit to within ~0.6 %). The 5 V/div range
  needs a 20-30 V reference, and its negative nonlinearity beyond 5 div would need about -30 V (the
  input limit), so it is only partially covered.
- A restricted wizard run (`--ranges`) still refits the global offsets in its zero step, which can
  move the ground of the other ranges by up to 1 count.
- A readout timeout at GUI startup was seen once and has not been reproduced (`--record FILE`
  captures it).
- ETS frames contain a few isolated outlier samples; `analysis.despike` removes them (ETS only).
