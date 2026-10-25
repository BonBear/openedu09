# openedu09

Open-source driver, GUI and MCP server for the **Whadda WSEDU09** (formerly Velleman EDU09)
USB PC oscilloscope kit.

[![CI](https://github.com/BonBear/openedu09/actions/workflows/ci.yml/badge.svg)](https://github.com/BonBear/openedu09/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/)


![openedu09 GUI showing a simulated 1 kHz signal](docs/images/gui.png)

*The GUI running against the built-in simulator (`--simulate`).*

**Naming:** the project and PyPI distribution are called `openedu09`; the Python import package
is `edu09` and the commands are `edu09-gui`, `edu09-mcp`, `edu09-probe` and `edu09-gain`.

Runs on Linux and Windows (Python 3.12+). The protocol was worked out for interoperability and then
verified on real hardware (see [docs/protocol.md](docs/protocol.md)).

## Supported hardware

- **Whadda WSEDU09** / Velleman **EDU09** (the same board, sold today as WSEDU09; USB HID, VID `10CF`, PID `ED90`), firmware **1.01** (verified).
- Other firmware versions and other Whadda/Velleman scopes are untested.

## Features

- **Driver library** (`edu09.Scope`): configure, single shot, continuous run, calibrated volts,
  14 timebases from 500 ms/div to 10 us/div (50/20/10 us use equivalent-time sampling), AUTO /
  NORMAL / off trigger with rising or falling edge.
- **GUI** (`edu09-gui`, PySide6 + pyqtgraph): live trace, cursors, measurements, CSV export,
  auto-set, calibration, optional built-in MCP server.
- **MCP server** (`edu09-mcp`): lets an AI agent configure the scope, acquire, measure and
  export waveforms, over stdio or streamable HTTP.
- **Simulator** (`--simulate`): everything works without hardware.
- **Safety**: one process owns the device at a time (file lock); calibration is opt-in and needs
  the input disconnected; `edu09-probe` blocks unknown opcodes.

## Quick start

```bash
git clone https://github.com/BonBear/openedu09.git && cd openedu09
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[gui]'          # or: uv pip install -e '.[gui]'
```

On Windows use `.venv\Scripts\activate` instead of `. .venv/bin/activate` (Windows is untested,
see [docs/install.md](docs/install.md)).

Linux needs a udev rule once (see [docs/install.md](docs/install.md)):

```bash
sudo cp packaging/linux/99-velleman-edu09.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Then:

```bash
edu09-gui                          # GUI on the attached scope
edu09-gui --simulate "sine:1khz:2vpp"   # GUI without hardware
edu09-mcp                          # MCP server on stdio
edu09-mcp --simulate               # MCP server on a simulated scope
edu09-mcp --http --port 8765       # MCP server on http://127.0.0.1:8765/mcp
```

### Claude Code MCP configuration

Real hardware over stdio (`.mcp.json` or `claude mcp add`):

```json
{
  "mcpServers": {
    "edu09": { "command": "/path/to/oscilloscope/.venv/bin/edu09-mcp" }
  }
}
```

Simulated scope (no hardware needed):

```json
{
  "mcpServers": {
    "edu09-sim": {
      "command": "/path/to/oscilloscope/.venv/bin/edu09-mcp",
      "args": ["--simulate", "square:1khz:3vpp"]
    }
  }
}
```

The GUI already owns the device (only one process can): start it with `edu09-gui --mcp-http`
(or Tools > MCP server) and point the client at its HTTP endpoint:

```json
{
  "mcpServers": {
    "edu09-gui": { "type": "http", "url": "http://127.0.0.1:8765/mcp" }
  }
}
```

Or `claude mcp add --transport http edu09-gui http://127.0.0.1:8765/mcp`.

**Bridge:** if the GUI already owns the scope and serves MCP, a plain stdio `edu09-mcp` (the first
config above) detects that and forwards to the GUI automatically, so one config works whether or
not the GUI is open. Only loopback owner URLs are accepted. Use `--no-bridge` to make `edu09-mcp`
exit (code 2) instead. If the owner serves no MCP URL, `edu09-mcp` exits with code 2 and a message
naming the owner.

**Debug recording:** `edu09-gui --record FILE` and `edu09-mcp --record FILE` append every USB
report exchanged with the scope to a JSONL file (useful for bug reports).

**Remote access:** the HTTP server binds to loopback only by default. `--host 0.0.0.0
--allow-remote` exposes it on the network, and `--allowed-host NAME[:PORT]` (repeatable) adds the
Host names clients will use. There is **no authentication or TLS**: anyone who can reach the port
controls the scope, so use it only on a trusted network (see [SECURITY.md](SECURITY.md)).

More in [docs/usage.md](docs/usage.md) (GUI tour, MCP tools, calibration, hardware tests).

## Safety notes

- Stay within the input rating of the EDU09 board (see the vendor manual); the driver cannot
  protect the hardware from over-voltage. Use the x1 probe settings assumed here.
- Calibration needs the input **disconnected** and rewrites the stored calibration (a backup is
  made). Do not run it with a signal connected.
- Only one program can use the scope at a time; the second one gets a clear "device busy"
  message naming the owner.
- The vertical gain is corrected per range (`range_gain` in `calibration.json`; typical values
  measured on one unit are the default when no calibration file exists). Measure your own unit
  against a DC reference with `edu09-gain` (docs/usage.md) for best accuracy.

## Project status and limitations

Alpha (0.1.0, unreleased). Known limitations:

- Vertical gain: per-range correction (`range_gain`) is supported; uncorrected units read
  0.4-2.6 % high. Large **negative** deflections (more than ~3-4 div below ground) still read up
  to ~6 % too large (front-end nonlinearity, under investigation).
- **Windows is untested** (no test machine); CI only runs the simulator-based tests.
- Intermittent readout timeouts at startup are known and under investigation.
- Single channel (CH1) only; other firmware versions untested.

## Verified hardware facts

Driver, simulator, GUI and MCP server are implemented and were exercised on one real EDU09
(firmware 1.01) with a 30 kHz 0-4.16 V square wave. Verified on hardware:

- HID discovery, version query `1.01`; no unsolicited reports while idle.
- Realtime sampling at 200 us/div measures 30 kHz within 0.3 %; ETS 50/20/10 us/div reconstruct
  clean waveforms (a few outlier samples; the GUI/analysis offer a despike option).
- Trigger polarity and edge selection; trigger point at a fixed sample index per mode.
- Running at about 28 frames/s; stop then acquire is clean; AUTO fallback works.
- Offset DAC settles in ~20 ms (the driver waits before arming); with the saved calibration 0 V
  reads within 0.05 div on all ranges and positions.
- The vendor's 0x0D "live" command does not return samples on fw 1.01 and is never sent.

- Vertical gain against a multimeter on all six ranges: reads 0.4-2.6 % high uncorrected (now
  corrected per range).

Not yet verified: Windows (no test machine), other firmware versions, linearity of large negative
deflections.

## Development

```bash
pip install -e '.[gui,dev]'
ruff check . && ruff format --check . && mypy && pytest -m "not hardware"
pytest --hardware -m hardware tests/hw     # needs the scope, see docs/usage.md
```

Layout: `src/edu09/{protocol,transport,device,mcp_server,gui}`, design in
[docs/specs/architecture.md](docs/specs/architecture.md).

## How it was built

The wire protocol was reverse engineered from the vendor software for the purpose of
interoperability, then confirmed with captures from a real device. The code was written
with a team of AI coding agents (see [AGENTS.md](AGENTS.md)) under human direction and review.

## Contributing

Issues and pull requests are welcome, see [CONTRIBUTING.md](CONTRIBUTING.md). Security reports:
[SECURITY.md](SECURITY.md). Changes: [CHANGELOG.md](CHANGELOG.md).

## License

[MIT](LICENSE), copyright (c) 2026 Björn Sjölén.

## Disclaimer

This project is independent. It is **not affiliated with or endorsed by Whadda or Velleman**.
"Whadda", "Velleman", "WSEDU09" and "EDU09" are trademarks of their respective owners. No vendor software, firmware, documentation or
other vendor files are included in this repository. Protocol knowledge was obtained for
interoperability purposes. Use at your own risk; the authors accept no liability for damage to
hardware or equipment.
