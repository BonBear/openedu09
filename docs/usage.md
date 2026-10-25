# Usage

## GUI (`edu09-gui`)

Options: `--simulate SPEC` (no hardware), `--mcp-http` (start the MCP server at launch),
`--port N`, `--no-run`, `--record FILE`, `-v`.

`--record FILE` appends every USB report exchanged with the scope to `FILE` (JSONL, one line per
report with a monotonic timestamp; flushed immediately, appended across runs and reconnects). Use
it to capture problems for a bug report; it changes nothing else (owner lock, calibration file
are used as without it).

Quitting during a calibration asks whether to wait or quit anyway; "quit anyway" aborts the
calibration at its next device access (nothing is saved, the scope is stopped). Closing the GUI
also aborts a calibration that an MCP client started through the GUI's MCP server.

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
  drag zoom/pan; View > Reset zoom (Ctrl+0).
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
- **Tools > Calibrate...**: disconnect the input first (see below).

## MCP server (`edu09-mcp`)

```
edu09-mcp [--http] [--host H] [--port N] [--allow-remote]
          [--allowed-host NAME[:PORT]] [--no-bridge] [--simulate [SPEC]]
          [--allow-calibration] [--export-dir DIR] [--record FILE] [--log-level L]
```

stdio by default (stdout is the protocol, logs go to stderr). `--http` serves streamable HTTP on
`127.0.0.1:8765/mcp`. Simulator specs: `sine:1khz:2vpp`, `square:50hz:5vpp:duty=25%:offset=2.5v`,
`dc:1.5v`, `noise:100mvrms`. `--record FILE` appends every USB report to a JSONL file (debugging;
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
| `export_csv` | write a stored waveform to a plain `*.csv` file in the export directory |
| `set_running` | start or stop continuous acquisition |
| `list_waveforms` | the last 32 stored waveforms |
| `auto_set` | pick V/div, timebase and trigger level for an unknown signal (100 mV..30 V, > 50 Hz) |
| `calibrate` | only with `--allow-calibration`; needs `confirm=true` and `input_disconnected=true` |

Resources: `edu09://status`, `edu09://capabilities`, and per-waveform JSON/CSV
(`edu09://waveform/{id}` and its CSV variant).

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

The ADC offset DACs differ between units, so a one-time calibration gives accurate 0 V and
position scaling. **Disconnect everything from CH1** (probe tip and ground clip free), then use
Tools > Calibrate in the GUI, or start `edu09-mcp --allow-calibration` and call `calibrate` with
`confirm=true, input_disconnected=true`. It takes a few seconds, backs up the old
`calibration.json` and saves the new one.

Values measured on the development unit (yours will differ slightly): base offset `0x86` (134),
range corrections `{1 V: -1, 0.1 V: -1}` DAC steps, ground fit slope k ~ 1.03 counts per DAC
step, b ~ -9.8. Plausible results: base offset within 0x5A..0xA6, k about 1.0. A failure message
starting with "CALIBRATION FAILED" usually means a voltage is connected. Afterwards 0 V should
read within +-0.1 div (we saw +-0.05 div) at every range and position. The offset calibration
keeps any stored gain corrections (below) unchanged.

### Gain (per range)

Uncorrected, the scope reads a few per cent high, by a different factor on each range. The
correction factor `g = V_true / V_read` per V/div range is stored in `calibration.json` under
`range_gain` (string keys in V/div; missing ranges use 1.0; accepted values `0.8 < g < 1.25`):

```json
{
  "version": 1,
  "calibration": {
    "adc_inverted": false,
    "base_ofs": 134,
    "counts_per_div": 32.0,
    "gnd_fit": {"b": -9.8, "k": 1.03},
    "range_corr": {"0.1": -1, "1": -1},
    "range_gain": {"0.1": 0.987, "0.2": 0.975, "0.5": 0.974, "1": 0.996, "2": 0.981, "5": 0.979},
    "source": "measured"
  }
}
```

(The values above are the development unit's; files without `range_gain` load as uncorrected.
The key `neg_nonlinearity` is reserved for a future correction of large negative deflections;
leave it out.) The file lives in the user config directory (`~/.config/edu09/calibration.json` on
Linux). Restart the GUI / MCP server after editing it by hand.

To measure a range: run the offset calibration first, connect a stable DC source (a battery or
a bench supply) to CH1, DC coupling, and measure it with a multimeter at the same time. Pick the
range so that the level is **2.4-6 divisions above ground** (positive voltages only; do not put
ground or the level at the screen edges / positions +-4), then, with the GUI and MCP server
closed:

```bash
edu09-gain --vref 4.95 --vdiv 1          # prints range_gain[1] = 0.99xx
edu09-gain --vref 4.95 --vdiv 1 --save   # also stores it (the old file is backed up)
```

It averages 4 free-run frames at 5 ms/div (trigger off) against the current calibration without
its gain and centres ground and level on screen (`--position` overrides the ground position).
From Python: `edu09.device.calibration.gain_from_reference(scope, v_ref, volts_per_div)` returns
the factor; `scope.set_calibration(scope.calibration.with_range_gain({1.0: g}), save=True)`
stores it.

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
