# Changelog

All notable changes are documented here. Format: [Keep a Changelog](https://keepachangelog.com/).

## [0.1.0] - unreleased

### Added
- GUI: Ctrl+wheel vertical zoom (and Shift+wheel pan) on the time plot, Ctrl+wheel dB zoom and
  double-click reset on the spectrum; display-only, reset by V/div/position changes and Ctrl+0.
- Protocol documentation from reverse engineering, verified on hardware (firmware 1.01).
- Driver: HID transport (hidraw on Linux, hidapi), protocol encoding/decoding, `Scope` with
  acquisition worker, single-process device ownership via a file lock.
- 14 timebases (500 ms/div to 10 us/div, ETS at 50/20/10 us), AUTO/NORMAL/off trigger, rising/falling edge.
- Simulator for hardware-free use and tests.
- `edu09-probe` diagnostic tool with opcode safety gates and JSONL capture.
- Calibration (offset DAC, ground fit) and autoset.
- MCP server (`edu09-mcp`): stdio and streamable HTTP (loopback by default).
- MCP bridge: a stdio `edu09-mcp` forwards to a GUI (or other process) that owns the device and
  serves MCP; loopback owner URLs only, `--no-bridge` to disable, exit code 2 with a clear message
  when the owner offers no MCP URL.
- Remote hardening: non-loopback `--host` requires `--allow-remote`; `--allowed-host NAME[:PORT]`
  for extra Host headers; DNS-rebinding protection; still no authentication.
- GUI (`edu09-gui`): live trace, cursors, measurements, CSV export, calibration, built-in MCP server.
- GUI follow-ups: acquisition-state indicator, confirm dialog when closing during calibration or
  auto-set, `libxcb-cursor` pre-flight message on Linux (`EDU09_SKIP_QT_CHECK=1` to skip).
- Hardware test suite (opt-in, `pytest --hardware tests/hw`), udev rule and installer script,
  install/usage docs.
- Calibration verified on hardware: base offset `0x86`, 0 V within 0.05 div at all ranges and
  positions; offset DAC settle wait (~20 ms) before arming.
- Spectrum analyzer: `analysis.spectrum` / `spectrum_average` / `spectrum_waveform` (FFT of the
  frame, Vrms/dBV with coherent-gain correction, hann/hamming/blackman/flattop/rect windows,
  scalloping-corrected peaks, fundamental, THD, power averaging and peak hold, warnings for
  aliasing on realtime timebases, clipped and ETS frames) and the MCP `spectrum` tool.

### Known limitations
- Windows verified with hardware (Python 3.12/3.13 only); macOS untested.
- Vertical gain is calibrated per unit by the wizard; full 5 V/div calibration needs `--max-volts 25`
  (negative nonlinearity beyond 5 div at 5 V/div would need -30 V, the input limit).
