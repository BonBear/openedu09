# Contributing

Thanks for helping. Bug reports, captures and calibration data from other units or firmware
versions, and pull requests are welcome. Please use the issue templates and include the output of
`edu09-probe list`.

## Development setup

```bash
git clone https://github.com/BonBear/openedu09.git && cd openedu09
python3 -m venv .venv && . .venv/bin/activate                  # Windows: .venv\Scripts\activate
pip install -e '.[gui,dev]'
```

Linux also needs the udev rule and, for the GUI, `libxcb-cursor0` (see [docs/install.md](docs/install.md)).
On Windows use Python 3.12 or 3.13. You can develop without hardware: everything runs on the
simulator (`--simulate`, `edu09.transport.sim`).

Layout (design in [docs/specs/architecture.md](docs/specs/architecture.md), protocol in
[docs/protocol.md](docs/protocol.md)):

| Path | Content |
|---|---|
| `src/edu09/protocol/` | opcodes, tables and pure codecs (no I/O) |
| `src/edu09/transport/` | HID (hidraw/hidapi), simulator, record/replay |
| `src/edu09/device/` | acquisition worker, thread-safe `Scope`, owner lock, calibration, auto-set |
| `src/edu09/calwizard/` | calibration wizard engine and `edu09-calibrate` |
| `src/edu09/mcp_server/` | MCP tools, stdio/HTTP server, bridge |
| `src/edu09/gui/` | PySide6 GUI |
| `src/edu09/{model,units,analysis,storage,probe}.py` | settings model, count/volt conversion, measurements and spectrum, persistence, `edu09-probe` |
| `tests/` | one directory per area; `tests/hw` needs a real scope |

## Checks

```bash
ruff check . && ruff format --check . && mypy
QT_QPA_PLATFORM=offscreen pytest -q          # hardware tests are skipped by default
```

CI runs the same commands on Linux and Windows (Python 3.12). Run `ruff format .` before
committing. Line length is 100; mypy is strict for `edu09.protocol`, `units`, `model` and
`analysis`. Tests never write your real config, data or cache directories (an autouse fixture
redirects `platformdirs`). Tests are marked `hardware` (skipped unless `--hardware` or `EDU09_HW=1`)
and `gui` (skipped when PySide6 is missing); `-m "not hardware"` and `-m "not gui"` select by marker.

## Hardware tests

`tests/hw` talks to a real scope and is skipped unless you pass `--hardware` (or set `EDU09_HW=1`).
Close the GUI and any MCP server first: tests skip with a message if the device is owned by
another process.

```bash
pytest --hardware -m hardware tests/hw -v                  # signal-free tests only
EDU09_HW_SIGNAL="square:30khz:0..4.16v" pytest --hardware -m hardware tests/hw -v
EDU09_HW_INPUT_OPEN=1 pytest --hardware -m hardware tests/hw -v
EDU09_HW_CALIBRATE=1 pytest --hardware -m hardware tests/hw/test_hw_calibration.py -v
```

The environment flags describe the physical setup; the tests cannot detect it, so set a flag only
when the setup matches:

| Flag | Meaning |
|---|---|
| (none) | discovery, version `1.01`, open/close, owner lock, free-run frames at 100 us..20 ms timebases, AUTO fallback and NORMAL timeout with an unreachable level, >= 20 fps for 2 s, stop then acquire |
| `EDU09_HW_SIGNAL=KIND:FREQ:LO..HI` | a known signal on CH1, DC coupled, e.g. `square:30khz:0..4.16v` (FREQ in hz/khz/mhz, levels in volts at the input). Enables NORMAL rising/falling trigger position (realtime: the table index +-2; ETS: the edge detected per frame), frequency +-3 % (realtime and ETS 50/20/10 us, where the period fits), loose Vpp check, unreachable-level behaviour, NORMAL-trigger fps. The signal tests fail once with a clear message if CH1 shows no signal |
| `EDU09_HW_INPUT_OPEN=1` | CH1 disconnected and a saved calibration: 0 V within +-0.1 div for all ranges at positions -3/0/+3, first frame after a position change is clean |
| `EDU09_HW_CALIBRATE=1` | CH1 disconnected: calibration dry run (`save=False`, previous calibration restored), compared with the saved one |

**`EDU09_HW_SIGNAL` and `EDU09_HW_INPUT_OPEN` are mutually exclusive** (a signal on CH1 and an
open CH1 contradict each other): setting both aborts the run with a usage error, so run the two
groups separately. `EDU09_HW_CALIBRATE` also needs CH1 disconnected, so do not combine it with
`EDU09_HW_SIGNAL`. The hardware tests only read your saved calibration; the calibration dry run
never saves and restores the previous calibration afterwards.

## Safety rules for the device

Unknown commands have unknown effects on the hardware.

- Never send unknown opcodes to a device outside the `edu09-probe` gates: acquire-class opcodes
  need `--allow-acquire`, `0x0C`, `0x14` and anything else need `--allow-unknown` plus an
  interactive typed confirmation (see [docs/usage.md](docs/usage.md#the-probe-tool-edu09-probe)).
- The driver itself may only emit the opcodes in `tables.DRIVER_OPCODES`; the encoder and
  `HidTransport.write` both enforce that. Do not add code paths that bypass this gate. The driver
  never sends `0x0D` (no live samples on firmware 1.01), `0x0C` or `0x14`.
- Calibration is opt-in: it needs the input disconnected and an explicit confirmation, saves with
  a backup, and an MCP agent may never invent multimeter readings.
- Keep inputs within the board's rating (30 V AC + DC at CH1). Do not run the vendor software
  while testing.

## Contributing captures and calibration data

- **Captures.** Record a session with `edu09-probe` (JSONL under `captures/`) or with
  `edu09-gui --record FILE` / `edu09-mcp --record FILE`. Include the firmware version, OS, input
  signal and settings. Place small, trimmed captures in `tests/fixtures/` and mention how they were
  made (see `tests/fixtures/h1/` and [docs/protocol.md](docs/protocol.md) section 11). Do not
  include vendor files or serial numbers you want to keep private.
- **Calibration data from your unit.** Attach your `calibration.json` and the wizard's session log
  (`calibration-sessions/<stamp>.json`; locations in [docs/install.md](docs/install.md#files-and-locations))
  to an issue, with the firmware version, whether RV1 was trimmed and the reference voltages and
  multimeter you used. Raw sweeps like [docs/data/edu09_gain_sweep_unit1.json](docs/data/edu09_gain_sweep_unit1.json)
  (counts per division per range, positive and negative side) are welcome too.

## Pull requests

Keep changes focused, add tests, update docs and `CHANGELOG.md`. Do not commit secrets or vendor
binaries. By contributing you agree your work is released under the MIT license.

## AI agents

Much of this repository was written by AI coding agents following the charter in
[AGENTS.md](AGENTS.md) (human-reviewed). You do not need any of that to contribute; the agents'
local memory files under `.agents/memory/` are git-ignored and never part of a pull request.
`.claude/agents` is a symlink to `.agents/agents`; on a Windows checkout with `core.symlinks=false` it
becomes a plain text file, which only affects Claude Code's subagent discovery.
