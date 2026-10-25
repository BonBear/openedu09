# Contributing

Thanks for helping. Bug reports, captures from other firmware versions and pull requests are welcome.

## Development setup

```bash
git clone https://github.com/BonBear/openedu09.git && cd openedu09
python3 -m venv .venv && . .venv/bin/activate                  # Windows: .venv\Scripts\activate
pip install -e '.[gui,dev]'
```

Linux also needs the udev rule and, for the GUI, `libxcb-cursor0` (see [docs/install.md](docs/install.md)).

## Checks

```bash
ruff check . && ruff format --check . && mypy
QT_QPA_PLATFORM=offscreen pytest -q          # hardware tests are skipped by default
```

CI runs the same commands on Linux and Windows. Run `ruff format .` before committing.

## Hardware tests

`tests/hw` needs a real EDU09 and is opt-in: `pytest --hardware -m hardware tests/hw` (or
`EDU09_HW=1`). Extra environment flags (`EDU09_HW_SIGNAL`, `EDU09_HW_INPUT_OPEN`,
`EDU09_HW_CALIBRATE`) describe the physical setup; set them only if it matches. Details in
[docs/usage.md](docs/usage.md#hardware-tests). Close the GUI/MCP server first (one process owns
the device).

## Safety rule

Never send unknown opcodes to a device outside the `edu09-probe` gates (`--allow-acquire`,
`--allow-unknown` plus typed confirmation). Unknown commands have unknown effects on the hardware.
Do not add code paths that bypass the transport opcode gate. Keep inputs within the board's rating.

## Contributing captures and fixtures

Use `edu09-probe` to record a session (JSONL). Please include firmware version, OS, input signal
and settings. Place small, trimmed captures in `tests/fixtures/` and mention how they were made.
Do not include vendor files or serial numbers you want to keep private.

## Pull requests

Keep changes focused, add tests, update docs and `CHANGELOG.md`. Do not commit secrets or vendor
binaries. By contributing you agree your work is released under the MIT license.
