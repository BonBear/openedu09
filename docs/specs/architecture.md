# EDU09 replacement software: architecture spec

> **Status: historical design document; implementation complete.** Written on 2026-10-24 by the
> architect before the hardware probing sessions. All tasks (T0-T13, H1) are done and the software is verified on
> real hardware (Linux and Windows, firmware 1.01). **Current behaviour is documented in
> [docs/usage.md](../usage.md) and [docs/protocol.md](../protocol.md) section 11** (hardware-verified;
> it wins wherever the two disagree). Sections 2.1-4 and 6 were updated as hardware findings arrived;
> section 8 (task and wave plan) and parts of section 9 describe the planning state.
>
> **Superseded by the current code** (the text below still shows the original plan in these places):
> - *Verification:* the "every protocol byte layout except `0x0F` is unverified" statement is obsolete;
>   see protocol.md section 11. `0x0D` is **not** a live mode (it only starts a status stream) and the
>   driver never sends it; status polling is passive (`tables.STATUS_POLL = "passive"`); the trigger
>   index is per mode (255/248/253/253) and detected per frame for triggered ETS frames, not 258.
> - *Calibration defaults:* `Calibration()` keeps the idealised `GndFit(k=2, b=-128)` for the
>   simulator and tests; hardware without a calibration file uses `Calibration.typical()`
>   (`base_ofs` 0x86, `k` ~1.03, `b` ~-9.8, no gain correction) and reports itself as uncalibrated
>   (`ScopeStatus.calibrated`, MCP `get_status`, GUI status bar), see 4.3.
> - *Package layout (section 2):* added `src/edu09/calwizard/` (wizard engine and `edu09-calibrate`),
>   `gui/{cal_wizard,spectrum,zoomview}.py`, `mcp_server/{cal_tools,netaddr}.py`, and
>   `src/edu09/probe.py`. Console scripts also include `edu09-calibrate`
>   and `edu09-gain` (`edu09.device.calibration:main`).
> - *MCP surface (section 6):* the `spectrum` tool, `export_csv(overwrite)`, `measure(despike)`,
>   `configure(trigger_auto_timeout_s)` and the `calibration_*` wizard tools were added later; the
>   exact payloads are in docs/usage.md and `mcp_server/tools.py`.
> - *Probe:* the `tools/probe.py` shim was removed; use `edu09-probe` or `python -m edu09.probe`.
>   There is no `probe.py capture` subcommand; captures come from `acquire`, `listen`, ...
>   and from `--record` on `edu09-gui` / `edu09-mcp`.
> - *ETS outliers (risk H1-b):* resolved as opt-in `analysis.despike` (GUI View > Despike, MCP
>   `despike` parameter), not applied by default.
> - Related specs: [calibration_wizard.md](calibration_wizard.md), [spectrum.md](spectrum.md).

Input at the time: `docs/protocol.md` (static RE; only the `0x0F` version query was hardware-verified).

Goals: a Python driver library, an MCP server for AI agents, and a desktop GUI for the Velleman EDU09
(USB HID 0x10CF:0xED90). Python 3.12 on Windows and Linux. **Every protocol byte layout except `0x0F` was
unverified when this was written**, so the design keeps protocol knowledge in one table module plus pure
codecs, and makes it cheap to record real traffic and replay it in tests.

---

## 1. Dependencies and packaging

| Need | Choice | Why |
|---|---|---|
| HID | **`hidapi`** (PyPI, cython-hidapi; `import hid`) | Ships binary wheels with the hidapi C library bundled for Windows and Linux, so no system lib or DLL is needed. The `hid` PyPI package (ctypes) requires a separately installed `libhidapi`/`hidapi.dll`, which is painful on Windows. The two packages both install a module named `hid`, so never install both. On Linux, prefer the **hidraw** backend (the `hidraw` module from cython-hidapi if present, otherwise `hid`). It keeps `hid-generic` bound and uses the existing udev rule. The libusb backend detaches the kernel driver and needs the `SUBSYSTEM=="usb"` rule line. Backend selection lives only in `transport/hid_transport.py`. |
| Arrays | numpy | |
| MCP | `mcp` (official SDK, `mcp.server.fastmcp.FastMCP`) | stdio plus streamable HTTP built in |
| GUI | PySide6 + pyqtgraph | optional extra `[gui]` |
| Paths | `platformdirs` | config, calibration and lock locations |
| Inter-process lock | `filelock` | cross-platform advisory lock (see 3.4) |
| Dev | pytest, pytest-qt, ruff, mypy | |

`pyproject.toml` (hatchling, src layout). Extras: `gui`, `dev`. Console scripts: `edu09-mcp`
(`edu09.mcp_server.__main__:main`), `edu09-gui` (`edu09.gui.app:main`), `edu09-probe` (`edu09.probe:main`,
also runnable as `python -m edu09.probe`; the original `tools/probe.py` shim was removed). Works with `uv sync --all-extras` and with `pip install -e .[gui,dev]`.
Linux needs `packaging/linux/99-velleman-edu09.rules` installed (already present). Windows needs no driver
because the device is a standard HID.

---

## 2. Package layout

```
src/edu09/
  __init__.py            # re-exports Scope, Settings, Waveform, enums, errors
  errors.py              # Edu09Error > DeviceNotFound, DeviceBusy, DeviceDisconnected, ProtocolError,
                         #   AcquisitionTimeout, Cancelled, CalibrationError, UnsafeOperation
  model.py               # enums, ChannelSettings/TriggerSettings/Settings, Waveform (pure, no numpy I/O)
  units.py               # Calibration dataclass, counts<->volts, trigger level byte, offset DAC, time axis
  analysis.py            # measurements + decimation (pure numpy)
  storage.py             # calibration JSON load/save (atomic), CSV export, WaveformStore (EDU09.ini import: removed)
  protocol/
    tables.py            # ALL magic numbers: opcodes, gain codes, timebase table, flag bits, status
                         #   marker, frame size, trigger index, ADC polarity, opcode safety classes
    commands.py          # encode_*() -> bytes (exactly 64-byte payload, no report-ID byte)
    reports.py           # parse_version, parse_status -> Status, FrameAssembler (pure state machine)
    ets.py               # deinterleave(mode, raw) / interleave(mode, samples) (inverse, for the simulator)
  transport/
    base.py              # Transport Protocol (below) + TransportInfo
    hid_transport.py     # HidTransport (hidapi), adds/strips report-ID byte, maps OSError -> Disconnected
    discovery.py         # list_devices() -> list[DeviceInfo]
    sim.py               # SimTransport + SimulatedDevice + SignalSpec (sine/square/triangle/dc/noise)
    replay.py            # RecordingTransport (wrapper, writes JSONL), ReplayTransport (reads JSONL)
  device/
    worker.py            # AcquisitionWorker thread: the ONLY code that touches a Transport
    scope.py             # Scope: public thread-safe API (futures + event callbacks)
    lock.py              # OwnerLock: inter-process device ownership + discovery of the owner's MCP URL
    calibration.py       # opt-in calibration procedure (runs on the worker via a command)
    autoset.py           # auto_set algorithm (uses Scope + analysis)
  mcp_server/
    tools.py             # register_tools(mcp: FastMCP, scope: Scope, opts) (shared by stdio/HTTP/GUI)
    server.py            # build_server(scope, opts) -> FastMCP; run_stdio(); run_http(host, port)
    bridge.py            # stdio<->HTTP proxy when another process owns the device
    __main__.py          # CLI: edu09-mcp [--http] [--port] [--simulate SPEC] [--allow-calibration] ...
  gui/
    app.py               # QApplication bootstrap, owns Scope, optional in-process MCP HTTP server
    qt_bridge.py         # ScopeQtAdapter: QObject turning Scope callbacks into queued Qt signals
    main_window.py       # layout, menus, status bar
    controls.py          # V/div, coupling, timebase, trigger, position, run/stop/single widgets
    plot.py              # pyqtgraph plot widget, cursors, measurements panel
tools/probe.py           # hardware probe + capture CLI (read-only by default; now src/edu09/probe.py)
tests/
  fixtures/              # *.jsonl captures + hand-written byte fixtures
  protocol/ transport/ device/ mcp/ gui/ hw/   # one directory per area (file ownership, sec. 7)
  conftest.py            # markers, --hardware option, sim fixtures
```

Dependency direction (strict, enforced by review): `protocol`, `model` -> `units`, `analysis` ->
`transport` -> `device` -> `mcp_server`, `gui`. `protocol` imports only the stdlib and `tables`. No module below `device`
starts threads. `gui` and `mcp_server` never import `transport` or `protocol` directly; they use `Scope`.

### 2.1 Key interfaces (contracts that let tasks proceed in parallel)

```python
# transport/base.py
class Transport(Protocol):
    info: TransportInfo                      # kind ("hid"|"sim"|"replay"), path, serial
    def open(self) -> None: ...
    def close(self) -> None: ...
    def write(self, payload: bytes) -> None: ...          # exactly 64 bytes; raises DeviceDisconnected
    def read(self, timeout_s: float) -> bytes | None: ... # 64 bytes, or None on timeout

# protocol/commands.py  (all return 64-byte payloads)
encode_simple(op: int) -> bytes          # only ops in tables.DRIVER_OPCODES, else UnsafeOperation
encode_version_query() / encode_reset() / encode_arm() / encode_readout() / encode_stop()
encode_live()                            # 0x0D: probe only; HW (H2) = fast status stream, never sent by the driver
encode_config(cfg: HwConfig) -> bytes    # HwConfig = raw byte fields (gain1, gain2, ofs1, ofs2, trig, tb, flags)

# protocol/reports.py
parse_version(r: bytes) -> str           # ASCII bytes 0..3 (verified)
parse_status(r: bytes) -> Status | None  # None if r[0] != STATUS_MARKER; Status(complete: bool, mode: int, raw)
class FrameAssembler:                    # fed reports after 0x0A; .feed(r) -> bool done; .frame -> bytes(1024)

# units.py
settings_to_hwconfig(s: Settings, cal: Calibration) -> HwConfig   # the ONLY Settings->bytes path
counts_to_volts(raw: np.ndarray, ch: ChannelSettings, cal: Calibration) -> np.ndarray
volts_to_counts(...)                     # inverse, used by the simulator and the trigger level
time_axis(n: int, tb: Timebase, trigger_index: int) -> np.ndarray
```

`tables.py` holds data only (dicts, frozen dataclasses, ints) keyed by the model enums' **values** (for
example `GAIN_CODES_CH1[0.5] = 0x01`), so `protocol` does not import `model`. Correcting a byte layout after
probing should normally touch only `tables.py` plus fixtures.

---

## 3. Concurrency and process model

### 3.1 One worker thread per device

`AcquisitionWorker` (a `threading.Thread`, daemon=True; an `atexit` hook stops/closes every live worker
after the non-daemon threads finished, so an un-closed Scope neither blocks interpreter exit nor leaves the
device armed) exclusively owns the `Transport`. Every other thread
talks to it through a `queue.Queue[Command]`. Each command carries a `concurrent.futures.Future`. Commands:
`Configure(settings)`, `Single(timeout_s, trigger_mode)`, `Run()`, `Stop()`, `ReadVersion()`,
`LiveLevel(n)` (an exclusive free-run frame capture, never `0x0D`), `Calibrate(...)`, `Shutdown()`.
Every wait loop in the worker is bounded twice: by an absolute deadline on the worker clock and,
independently of any clock, by summed read timeouts plus a maximum report count, so a device that keeps
streaming reports can never hang it (H2, protocol.md 11 finding 21).

Worker states: `DISCONNECTED -> IDLE -> ARMED -> READING -> IDLE`, with `RUNNING` meaning "re-arm after
each frame". Loop:

```
while not shutdown:
    cmd = queue.get(timeout = 0 if state in (ARMED, READING) else 0.05)   # drain commands first
    handle(cmd)                      # Configure while ARMED: send 0x0E, then 0x09,0x0B (as the original does)
    if state == ARMED:   r = t.read(POLL_READ_TIMEOUT=0.02); status = parse_status(r) ...
                         complete & mode matches -> send 0x0A, FrameAssembler, state = READING
                         trigger deadline passed -> AUTO: re-send config with trigger off for 1 frame;
                                                    NORMAL/Single: fail future with AcquisitionTimeout
    if state == READING: r = t.read(FRAME_READ_TIMEOUT=0.2); assembler.feed(r)
                         done -> decode -> Waveform -> publish; RUNNING ? (0x09,0x0B, ARMED) : IDLE
                         timeout -> resync(): send 0x08, 0x09, drain reads until None; raise ProtocolError
```

* Reads are always short-timeout, so a `Stop`, `Configure` or `Shutdown` is serviced within about 20 ms.
  **Cancellation** = `Stop()` (send `0x08`, fail pending `Single` futures with `Cancelled`), or
  `future.cancel()` from the caller (the worker checks `future.cancelled()` each loop).
* **Poll strategy** (`tables.STATUS_POLL`): Q1 is answered on hardware - the device streams status reports
  after `0x0B` ("passive": all-zero waiting report every ~33 ms, `64 C8 mm` on completion). The worker's
  `self._poll_status()` just reads; `"resend_arm"` remains only as a simulator option.
* **Publishing**: each `Waveform` gets a monotonically increasing `seq`. The worker calls subscriber
  callbacks (`Scope.subscribe(cb) -> unsubscribe`) from the worker thread. Callbacks must be cheap and never
  block; the GUI adapter re-emits as a queued Qt signal, and the MCP layer only stores into a `WaveformStore`.
  Exceptions in callbacks are logged and swallowed.
* **Single while running**: `Scope.acquire()` during RUNNING does not restart the hardware. It returns the
  first frame whose `seq` is newer than the request and whose settings snapshot equals the current settings.

### 3.2 Unplug and reconnect

Any `OSError`/hidapi error maps to `DeviceDisconnected`. The worker closes the transport, fails in-flight
futures with `DeviceDisconnected`, emits `ConnectionEvent(connected=False)`, and enters DISCONNECTED. In
that state it retries `open()` every 1 s (`Scope(auto_reconnect=True)` default). On success it re-reads the
version, re-applies the last `Settings`, restores RUNNING if it was running, and emits
`ConnectionEvent(True, firmware)`. Commands issued while disconnected fail immediately with
`DeviceDisconnected` (they are not queued), except `Configure`, which only updates the stored settings.

### 3.3 Scope API (sync facade, thread-safe)

```python
Scope.open(transport: Transport | None = None, *, auto_reconnect=True, calibration=None) -> Scope
scope.close()                         # Stop, optional 0x14 (tables.SEND_RELEASE_ON_CLOSE, default False), join
scope.status -> ScopeStatus           # connected, firmware, state, settings, last_seq, calibration_source
scope.settings -> Settings
scope.configure(**partial) -> Settings              # validates (ValueError with allowed values), applies
scope.acquire(timeout_s=2.0, trigger_mode=None) -> Waveform   # single shot (blocking, cancellable future variant)
scope.acquire_async(...) -> Future[Waveform]
scope.run() / scope.stop()
scope.subscribe(cb: Callable[[Event], None]) -> Callable[[], None]   # Waveform | ConnectionEvent | SettingsEvent
scope.live_level(n=1024, *, timebase=None) -> float  # mean raw count (never volts) of n free-run samples
                                      # (trigger off, current V/div/offset, 200 us/div default; settings
                                      # restored). Used by T9 calibration. Never sends 0x0D (H2)
scope.calibrate(*, confirm: bool, input_disconnected: bool) -> Calibration   # see sec. 5
```

### 3.4 Sharing one device between the GUI and the MCP server (decision)

hidraw on Linux and hidapi on Windows (FILE_SHARE_READ|WRITE) do **not** stop two processes opening the
device. Interleaved polling would corrupt frames silently. Decision:

1. **Exactly one owner process per device**, enforced by `device/lock.py`: a `filelock` at
   `platformdirs.user_runtime_dir("edu09")/device.lock` (Windows: the user cache dir), plus a sidecar
   `owner.json` holding `{pid, kind: "gui"|"mcp", mcp_url | null, started}`. `Scope.open` with a hardware
   transport takes the lock or raises `DeviceBusy(owner_info)`. Sim/replay transports skip the lock.
2. **GUI = owner + optional in-process MCP server.** `edu09-gui` creates the `Scope` and can serve the
   same `Scope` over **streamable HTTP** at `http://127.0.0.1:8765/mcp` (menu toggle or `--mcp-http`). The server
   runs in a background thread (uvicorn via FastMCP `streamable_http_app()`), and the URL goes into `owner.json`.
   The agent and the human then see one device and one settings state. Settings changed by an agent emit
   `SettingsEvent`, which the GUI controls reflect (last writer wins; the GUI status bar shows "changed by MCP").
3. **Headless = standalone `edu09-mcp`** (stdio by default, `--http` optional). It is the owner.
4. **Bridge**: if `edu09-mcp` (stdio) finds the device owned by a process whose `owner.json` has an
   `mcp_url`, it runs `bridge.py`. That is a stdio MCP server whose tools forward to the owner via the SDK's
   streamable-HTTP client, so an agent's config (`edu09-mcp` over stdio) works whether or not the GUI is open. Only loopback owner URLs are accepted, and `--no-bridge`
   disables forwarding. If the owner has no usable URL, `edu09-mcp` prints the owner pid/kind and exits with
   code 2 (no tools are served).

Justification: in-process sharing avoids inventing an IPC protocol. MCP over HTTP *is* the IPC, and the
bridge is a thin, testable adapter. HTTP binds 127.0.0.1 only; `--host 0.0.0.0` requires
`--allow-remote` and logs a warning (there is no auth in v1); extra Host headers are listed with
`--allowed-host NAME[:PORT]`.

---

## 4. Data model

### 4.1 Enums and settings (`model.py`)

```python
class VoltsPerDiv(float, Enum): V0_1=0.1; V0_2=0.2; V0_5=0.5; V1=1.0; V2=2.0; V5=5.0
class Coupling(str, Enum): AC="ac"; DC="dc"; GND="gnd"
class Timebase(float, Enum): seconds/div: 500e-3, 200e-3, 100e-3, 50e-3, 20e-3, 10e-3, 5e-3, 2e-3, 1e-3,
                              500e-6, 200e-6, 100e-6, 50e-6, 20e-6, 10e-6     # names like MS2, US50
class TriggerEdge(str, Enum): RISING="rising"; FALLING="falling"
class TriggerMode(str, Enum): AUTO="auto"; NORMAL="normal"; NONE="none"   # NONE = hw trigger off (free run)

@dataclass(frozen=True) ChannelSettings: volts_per_div=V2, coupling=DC, position_div: float = 0.0  # [-4, 4]
@dataclass(frozen=True) TriggerSettings: mode=AUTO, edge=RISING, level_v: float = 0.0, auto_timeout_s=0.1
@dataclass(frozen=True) Settings: ch1: ChannelSettings, timebase=MS2, trigger: TriggerSettings
    def replace(self, **partial) -> Settings   # flat keys: volts_per_div, coupling, timebase, trigger_mode, ...
    def to_dict() / from_dict()                # JSON-friendly; used by MCP + storage
```

* Validation happens in `__post_init__`/`from_dict`: unknown values raise `ValueError` listing the allowed
  values. Floats are accepted and snapped to an enum member only if within 1% (`VoltsPerDiv.parse("0.5")`,
  `Timebase.parse("2ms")`). Clamps are not silent: out-of-range `position_div`/`level_v` raise.
* ETS timebases (50/20/10 us) force `trigger.mode != NONE` (help: ETS needs a trigger). `configure` returns a
  warning string instead of failing when it coerces this.
* CH2 is not modelled (the EDU09 is single-channel). `tables` holds neutral CH2 bytes (DC, 2 V/div, offset 0x80).
* GND coupling: the hardware gets DC; decode replaces the samples with the calibrated ground count (0 V).
  The original filled with the frame mean; we deliberately differ, and note it in the docs.

### 4.2 Waveform

```python
@dataclass(frozen=True, eq=False)
class Waveform:
    seq: int
    timestamp: datetime            # UTC, when the frame completed
    raw: np.ndarray                # uint8[1024], de-interleaved, read-only (flags.writeable=False)
    volts: np.ndarray              # float64[1024]
    dt: float                      # s/sample (effective rate for ETS)
    trigger_index: int             # units.trigger_index(tb) = tables.TRIGGER_INDEX_BY_MODE[mode] (HW: 255/248/253/253);
                                   # triggered ETS frames: the edge detected per frame within table +-8
                                   # (worker.detect_trigger_index, protocol.md 11.4), else the table value
    settings: Settings             # snapshot used for this frame
    calibration_id: str            # hash/source of the Calibration used
    acq_mode: int                  # status mode byte (1 realtime, 2-4 ETS)
    triggered: bool                # False if AUTO fell back to free run
    clipped: bool                  # any raw == 0 or 255
    def t(self) -> np.ndarray      # (arange(n) - trigger_index) * dt
```

Decode pipeline (pure, in `device/worker.py::decode_frame` calling pure helpers):
`frame bytes -> np.frombuffer -> ets.deinterleave(acq_mode) -> (GND fill) -> units.counts_to_volts`.
ETS reordering is a pure function selected by the **mode byte reported in the status**, not by the
requested timebase. A mismatch between the two raises `ProtocolError`, so a wrong table entry fails loudly.
No x4 upsampling or smoothing happens in the driver; that is a display concern.

### 4.3 Units and calibration

`Calibration` (frozen): `base_ofs=0x80`, `range_corr: dict[float,int]`, `gnd_fit=(k=2.0, b=-128.0)` (fit in the "ad = 255 - raw" domain against the absolute DAC byte, per protocol.md 5.3; 0 V -> raw 127, trigger byte 127),
`counts_per_div=32`, `adc_inverted=tables.ADC_INVERTED`, `source: "default"|"typical"|"file"|"measured"` (the planned `"ini-import"` and its vendor-INI importer were removed, never exposed; `from_dict` loads it as `"file"`),
`range_gain: dict[float,float]` (per-range gain correction `g = V_true / V_read`, `0.8 < g < 1.25`, missing = 1.0;
like `range_corr` excluded from `hash()`, JSON keys are strings; since the calibration wizard it is the
**positive-side** gain), `range_gain_neg` (negative-side gain, missing = `range_gain`), `neg_nonlinearity`
(per range piecewise-linear knots `(r, e)` of the extra stretch of large negative deflections, applied;
calibration_wizard.md 1.2) and `meta` (provenance, not part of `calibration_id` or equality). Every counts <->
volts path goes through `units.deflection_to_volts` / `volts_to_deflection` (exact inverses). Files with only v1
keys are written as version 1, otherwise 2; `from_dict` still rejects unknown keys.
Gain: `Calibration.effective_counts_per_div(vdiv) = counts_per_div / range_gain_for(vdiv)` is used by every
counts <-> volts path (`counts_to_volts` multiplies by g, `volts_to_counts`/`volts_to_raw`/`trigger_byte` divide)
and by the position -> offset DAC conversion, so the ADC window stays centred on the screen. `level_range_v()`
(model, calibration-free) stays the 8-division screen window; with g < 1 the ADC span is up to ~2.6 % smaller
than the screen (it saturates just inside the edges), and an out-of-ADC trigger level is clamped to 0/255. HW-measured gains
(one unit, 2026-10-25) are `tables.TYPICAL_RANGE_GAIN` (not applied by default; `Calibration.typical()` has no gains). The offset
calibration (`measure_calibration`) carries `range_gain`/`neg_nonlinearity` over unchanged;
`calibration.gain_from_reference` (CLI `edu09-gain`) measures one range against a DC reference. The
**calibration wizard** (`edu09.calwizard`, spec `docs/specs/calibration_wizard.md`) plans and runs a full
per-unit calibration (zero + ground map, optional RV1 trimmer step, per-range positive/negative gain and
negative nonlinearity, fit, save with backup, verify) against the user's multimeter readings; front ends
`edu09-calibrate` (CLI), the GUI's `CalibrationWizard` (`gui/cal_wizard.py`, Tools > Calibrate) and the MCP
`calibration_*` tools (`mcp_server/cal_tools.py`).
`Calibration()` is the idealised model of the original's demo mode; the simulator's front end and the test
fixtures use it, so sim/replay scopes keep it. Real units have a DAC slope of only ~1.03 counts per step
(HW-measured 2026-10-24: `base_ofs=0x86`, `range_corr={1 V: -1, 0.1 V: -1}`, `k~1.03`, `b~-9.8`), so with
`Calibration()` positions on hardware would be ~2x off: `Scope.open` on a hardware (`hid`) transport without a
usable calibration file uses `Calibration.typical()` (those values, `source="typical"`, from
`tables.TYPICAL_*`, no gain correction; logged and reported as uncalibrated); a per-unit calibration
file always wins.
Offset settling (HW-verified): after a `0x0E` that changed the offset byte (or the CH1 gain/coupling byte, or
when the device config is unknown) the worker waits `tables.OFFSET_DAC_SETTLE_S` (20 ms, worker clock, by
reading and discarding IN reports) before `0x0B`; unchanged configs (continuous running, AUTO free-run
fallback) add no delay. `DeviceIO.live_samples` settles the same way (plus an optional `settle_s`).
`settings_to_hwconfig` computes CH1OFS with the protocol.md 5.3 formula (the position is converted from
divisions to DAC steps through `k`), and the trigger byte from `level_v` via `volts_to_counts` (and the
`255 - x` rule in tables). The polarity and the 258 trigger index are single constants in `tables.py` (Q3, Q4).

---

## 5. Safety

* The driver can only emit opcodes in `tables.DRIVER_OPCODES = {0x08,0x09,0x0A,0x0B,0x0D,0x0E,0x0F}`
  (`0x14` is gated by `SEND_RELEASE_ON_CLOSE=False` until probed; `0x0C` never; `0x0D` stays allowed for
  the probe but the device core never sends it, H2). `encode_simple` raises
  `UnsafeOperation` otherwise; `HidTransport.write` asserts the same as a second line of defence.
* Calibration in the original is **host-side** (results go to `EDU09.ini`; the device only receives offset
  DAC bytes). It is still opt-in because it overwrites stored calibration and needs a disconnected input:
  `Scope.calibrate(confirm=True, input_disconnected=True)` (both required, else `UnsafeOperation`). It
  writes `calibration.json` atomically (tmp + `os.replace`) after backing up the previous file as
  `calibration.<timestamp>.bak.json`. It never touches the vendor `EDU09.ini` (the planned read-only
  import was removed).
* MCP: the `calibrate` tool and the `calibration_*` wizard tools are **not registered** unless the server
  starts with `--allow-calibration`; even then `calibrate` and `calibration_finish(save=true)` require
  `confirm=true`, the zero step requires `input_disconnected=true`, and reference readings must be typed by
  the human (tool descriptions forbid the agent to invent them; `|reading_v| <= 30` V). One wizard session per
  server (and per Scope), aborted after 30 min idle and on server shutdown; nothing is installed before save.
* Calibration wizard: nothing is installed or saved before an explicit save (`save(confirm=True)`), abort is
  non-blocking (`Scope.abort_exclusive`) and never needs a restore; the GUI runs every session call off the
  GUI thread and keeps the "input disconnected" confirmation for the zero step. Reversing a source is only
  offered for floating sources (warning in every plan with a reversible source). `export_csv` only writes inside the export dir (default
  `user_data_dir/edu09/exports`; `..`/absolute paths are rejected).
* `edu09-probe` (planned as `tools/probe.py`) is **read-only by default** (sec. 7.1).

---

## 6. MCP surface (`mcp_server/tools.py`)

Server name `edu09`. Transports: **stdio** (default) and **streamable HTTP** (`--http`, 127.0.0.1:8765,
path `/mcp`). `--simulate "sine:1khz:2vpp"` uses SimTransport (for agents/CI without hardware). All tools
return JSON objects. Errors are raised as tool errors whose message is `"<ErrorClass>: <human hint>"` (for
example `"AcquisitionTimeout: no trigger within 2.0 s; try trigger_mode='auto' or lower level_v"`). Numeric
floats are rounded to 4 significant digits in payloads. Hard caps: `max_points <= 2048`, response
target under 32 KB.

| Tool | Params | Returns |
|---|---|---|
| `get_status` | — | `{connected, firmware, owner:{kind,pid}, state:"idle"\|"running"\|"armed"\|"disconnected", settings, last_waveform_id, calibration_source, simulated}` |
| `get_capabilities` | — | `{volts_per_div:[...], timebases_s_per_div:[...], couplings, trigger_modes, trigger_edges, position_div_range, samples_per_frame:1024, divisions_per_frame:32.8, ets_timebases:[...], measurements:[...], limits:{max_points:2048}}` |
| `configure` | all optional: `volts_per_div: float, coupling: str, timebase: str\|float` (e.g. `"2ms"` or `0.002`), `trigger_mode, trigger_edge, trigger_level_v: float, position_div: float, trigger_auto_timeout_s: float` | `{settings, warnings:[str]}`; partial update, validated atomically (all or nothing) |
| `acquire` | `timeout_s=2.0` (0.1–30), `max_points=200` (16–2048), `trigger_mode: str\|None` | `{waveform_id, timestamp, triggered, clipped, settings, sample_rate_hz, duration_s, stats:{min,max,mean,vpp,vrms,frequency_hz\|null}, points:{t0_s, sample_dt_s, decimation, v:[...], and dt_s (evenly spaced) or i:[...] sample offsets (minmax)}, warnings}` |
| `get_waveform` | `waveform_id="latest"`, `start=0`, `stop=1024`, `max_points=1024`, `decimation="minmax"\|"stride"`, `include_raw=false` | `{waveform_id, <points as in acquire>, raw:[...]?}`; index range in samples |
| `measure` | `waveform_id="latest"`, `measurements: list[str] \| None` (all if None) from `vpp,vmax,vmin,mean,vrms,ac_rms,frequency,period,duty,rise_time,fall_time,pos_width,neg_width`, `despike=false` | `{waveform_id, results:{name:{value,unit,valid,note?}}, despiked}` |
| `spectrum` | `waveform_id="latest"`, `window="hann"`, `max_points=512`, `peaks=8`, `db_floor=-80`, `despike=false` (added with the spectrum analyzer, [spectrum.md](spectrum.md)) | `{fs_hz, resolution_hz, n, dc_v, fundamental, thd_pct, peaks, points, warnings, ...}` |
| `export_csv` | `waveform_id="latest"`, `filename: str\|None`, `overwrite=false` | `{path, rows, columns:["t_s","volts","raw"]}` (header carries settings as `#` comments) |
| `set_running` | `running: bool` | `{state}`: continuous acquisition (feeds GUI and `latest`) |
| `auto_set` | `timeout_s=5.0` | `{settings, rationale:[str], waveform_id}` |
| `list_waveforms` | — | `[{waveform_id, timestamp, timebase, volts_per_div}]` (store keeps the last 32) |
| `calibrate` *(only with `--allow-calibration`)* | `confirm: bool`, `input_disconnected: bool` | `{calibration, backup_path}` (offsets only) |
| `calibration_plan` *(flag)* | `sources: [{label, volts? , min_v?, max_v?, tolerance_pct=5, reversible=false}]` (<= 8), `mode`, `max_abs_v=15`, `trimmer=false` | `{setups, coverage, reconnections, user_actions, estimated_s, warnings}`; pure |
| `calibration_start` *(flag)* | as plan + `uncalibrated="keep"` | `{session_id, plan, step}`; one session at a time |
| `calibration_step` *(flag)* | — | `{session_id, step:{state, index, total, setup:{id, kind, instruction, volts, needs}, needs, phase, last_result, live?, sim_reading_v?, warnings}}`; trimmer live phase: one sample per call |
| `calibration_submit` *(flag)* | `action: measure\|retry\|skip\|done`, `reading_v`, `input_disconnected=false` | `{result?:{status, points:[{vdiv, position_div, deflection_div, err_pct, status, code, message}], messages}, step}` |
| `calibration_finish` *(flag)* | `save=false`, `confirm=false` | `{preview}`; with `save=true, confirm=true` also `{saved, backup_path, calibration_id, warnings}` |
| `calibration_abort` *(flag)* | — | `{aborted, log_path}` |

Wizard tool payloads are capped at 8 KB (no raw samples; plan instructions are shortened if needed). Wizard
tool descriptions say: "Readings must be typed by the human from their multimeter; never invent or estimate
them. Ask the human to perform every physical step." `edu09-mcp --simulate ... --sim-unit unit1` simulates a
unit with real gain errors for trying them.

Resources: `edu09://status`, `edu09://capabilities`, `edu09://waveforms/{waveform_id}` (JSON, full
resolution), `edu09://waveforms/{waveform_id}.csv`. Waveform ids are `"w<seq>"`. Older ids return
`NotFound: waveform evicted (store holds last 32)`.

Decimation (`analysis.decimate`): `minmax` emits pairs (min, max) per bucket in time order, so peaks and
glitches survive; `stride` takes every k-th sample. Stats and measurements are always computed on the full
1024 samples. Measurement validity: frequency/period/duty need at least 2 full periods (mean-crossing with
10% Vpp hysteresis), and rise/fall use 10–90%. Otherwise `valid=false` with a note. Clipping adds the warning
`"clipped: increase volts_per_div"`.

`auto_set` (from the help: 100 mV–30 V, >50 Hz, duty >10%): set trigger AUTO and DC. Step V/div from 5 V
downward and keep the smallest range where Vpp < 6 div and nothing is clipped. Then pick the realtime
timebase that puts 4–20 periods in the 1024-sample frame (no ETS unless the frequency exceeds about 10 kHz).
Set the trigger level to the mean. Every decision is appended to `rationale`.

---

## 7. Testing strategy

| Layer | Tests (dir) | How |
|---|---|---|
| protocol | `tests/protocol/` | Byte fixtures: exact `encode_config` bytes for protocol.md 10.3 (`0E 80 07 0A 04 80 80 80 08 02`), version reply `b"1.01"+garbage`, status parsing, FrameAssembler with leading status reports and a first data byte of 0x64, ETS interleave/deinterleave round-trip (hypothesis optional) |
| units / model / analysis | `tests/units/` | Volts round-trip, enum parsing/validation errors, measurements on analytic numpy signals (sine/square with known f, duty, Vrms), decimation preserves extrema |
| transport | `tests/transport/` | Replay of `tests/fixtures/*.jsonl` captures; HidTransport with a mocked `hid` module (report-ID prefixing, error mapping) |
| simulator | `tests/device/` | `SimulatedDevice` implements the *device side* of the protocol from `tables` (status reports while armed, `64 C8 mm` after N polls, 16 data reports produced by `volts_to_counts` + `ets.interleave`). The tests then exercise the **real** worker, assembler, ETS and units path: a 1 kHz sine at 1 V amplitude gives measured f within 1% and Vpp within 3%. Fault injection: drop a report, unplug mid-frame, never trigger, slow status. |
| device | `tests/device/` | Worker states, cancel latency < 100 ms, reconnect re-applies settings, AUTO fallback, OwnerLock contention (two Scopes, second gets `DeviceBusy`) |
| MCP | `tests/mcp/` | In-memory client (`mcp.shared.memory.create_connected_server_and_client_session`) against a sim-backed Scope: tool list (no `calibrate` without the flag), schemas, payload size < 32 KB at max_points=2048, error messages, the bridge against an in-process HTTP server |
| GUI | `tests/gui/` | pytest-qt with `QT_QPA_PLATFORM=offscreen`, sim transport: controls -> settings, waveform signal updates the plot, MCP-originated settings change updates the controls |
| hardware | `tests/hw/` | `@pytest.mark.hardware`, skipped unless `--hardware` or `EDU09_HW=1`. Version, config+single acquire at each timebase, captures saved as new fixtures |

Markers live in `pyproject.toml` (`hardware`, `gui`, `slow`). CI runs on ubuntu and windows with Python 3.12:
ruff, mypy (strict for `protocol`, `units`, `model`, `analysis`), and pytest without hardware.
**Capture -> fixture loop:** `probe.py capture` writes JSONL that `ReplayTransport` and the protocol tests can
consume directly. Each verified finding adds a fixture and a `tables.py` edit, and protocol.md is updated by
`janitor`.

### 7.1 `tools/probe.py`

> Now `src/edu09/probe.py`; the `tools/probe.py` shim was removed. Use `edu09-probe` or
> `python -m edu09.probe`.

Subcommands: `list` (enumerate HID devices), `version` (0x0F), `listen --seconds N` (read only), `config
HEX...` / `acquire --timebase 2ms --vdiv 1 --count N`, `send HEX`, `replay FILE`. All of them log every
OUT/IN report as JSONL `{"t": mono_s, "dir": "out"|"in", "hex": "...", "note": "..."}` to
`captures/<date>-<cmd>.jsonl` (git-ignored) and pretty-print the hex. Safety classes come from
`tables.OPCODE_CLASS`: `read` = {0x0F} is always allowed; `acquire` = {0x08,0x09,0x0A,0x0B,0x0D,0x0E}
needs `--allow-acquire`; anything else (0x0C, 0x14, unknown) needs `--allow-unknown` **and** an interactive
typed confirmation. The default is read-only. The probe uses `protocol` and `transport` only (never
`device`), so it works while the higher layers are broken.

---

## 8. Milestones and tasks (file ownership = no two parallel tasks share files)

*The original task and wave plan (T0-T13, H1; six waves with strict file ownership and model tiers per
task) was an orchestration aid and is complete; the task ids survive in code comments and in section 9.
Where each task ended up:*

| ID | Task | Implemented in |
|---|---|---|
| T0 | Scaffold (pyproject, errors, test config, CI) | `pyproject.toml`, `src/edu09/errors.py`, `tests/conftest.py`, `.github/workflows/ci.yml` |
| T1 | Protocol | `src/edu09/protocol/`, `tests/protocol/` |
| T2 | Model and units | `src/edu09/model.py`, `src/edu09/units.py` |
| T3 | Transport | `src/edu09/transport/{base,hid_transport,discovery,replay}.py` |
| T4 | Probe CLI | `src/edu09/probe.py` |
| T5 | Analysis | `src/edu09/analysis.py` |
| T6 | Simulator | `src/edu09/transport/sim.py` |
| T7 | Device core | `src/edu09/device/{worker,scope,lock}.py` |
| T8 | Storage | `src/edu09/storage.py` |
| T9 | Calibration and auto-set | `src/edu09/device/{calibration,autoset}.py`, later `src/edu09/calwizard/` |
| T10 | MCP server | `src/edu09/mcp_server/{tools,server,__main__}.py` |
| T11 | GUI | `src/edu09/gui/` |
| T12 | Bridge and owner discovery | `src/edu09/mcp_server/bridge.py` |
| H1 | Hardware probing sessions (answered Q1-Q10) | `tests/fixtures/h1/`, `docs/protocol.md` section 11 |
| T13 | Hardware tests and docs | `tests/hw/`, `README.md`, `docs/install.md`, `docs/usage.md` |

Project milestones: M3 = T0-T8, M4 = T9, T10, T12, M5 = T11, M6 = H1, T13 (all done).

## 9. Risks and open protocol questions

| # | Question / risk (protocol.md 9) | Isolation in the design |
|---|---|---|
| Q1 | **Answered (H1, 2026-10-24; protocol.md 11)**: nothing unsolicited when idle; after `0x0B` the device streams by itself: an all-zero 64-byte waiting report every ~33 ms (`tables.STATUS_INTERVAL_S`) while waiting for a trigger, and `64 C8 mm` + zeros as soon as the capture completes. After `0x0A`: 1-2 leading `64 C8 mm`, then 16 data reports. | `tables.STATUS_POLL = "passive"` (HW-verified; the worker just reads, never re-sends `0x0B`); waiting report = `reports.is_waiting_report` (never a `Status`, R1); simulator default is passive with the HW timing, `resend_arm` kept as a sim option only |
| Q2 | Meaning of status bytes 1 (not C8) and 3..63. **Resolved (H1/H2)**: byte 1 is only ever `C8`; bytes 3..63 are always zero (also in the `0x0D` stream); waiting has no marker. No live bytes exist anywhere: `0x0D` is a fast status stream, not live samples | `Status.raw` is kept; parse only bytes 0–2; live levels come from free-run frames |
| Q3 | ADC polarity, GND count at offset 0x80. **Answered (H1)**: higher voltage = higher raw (`ADC_INVERTED=False`), trigger byte not inverted; GND ~132 raw at 0x80 (model 127, left to calibration) | `tables.ADC_INVERTED`, `Calibration.gnd_fit`; all in `units.py` |
| Q4 | Trigger index. **Answered (H1)**: not 258 but per mode 255 (realtime) / 248 (ETS x8) / 253 (ETS x16) / 253 (10 us), +-1 | `tables.TRIGGER_INDEX_BY_MODE`, `units.trigger_index(tb)`; `Waveform.trigger_index` carried per frame (ETS: detected per frame, the position varies +-4 between sessions, protocol.md 11.4); the simulator places the trigger there |
| Q5 | Edge bit polarity, digital-mode bit. **Answered (H1)** for bit 2: set = falling. Bit 3 untested | `tables.FLAG_*`; digital mode not exposed |
| Q6/Q7 | Replies to `0x08`/`0x14`/`0x0C`; safety of `0x0A` early or `0x0E` while armed | Opcode classes; `SEND_RELEASE_ON_CLOSE=False`; worker sends `0x0A` only after a valid status; `resync()` drains |
| Q8 | Version format | Verified: ASCII `1.01` + CR, then garbage; `parse_version` reads up to the CR |
| Q9 | CH2 config bytes | Neutral constants in tables |
| Q10 | Exact sample rates. **Verified (H1)** for 200 us, 50 us, 20 us and 10 us/div (10 us = 1.5625 MHz, same as 20 us) | `tables.TIMEBASES[tb].sample_rate_hz`; measurements depend only on `dt` |
| H1-a | Each read-out ends with one extra (17th) data-like report that is not frame data (protocol.md 11, finding 18) | Worker must drain it after the frame (or flush before the next `0x0B`) and ignore non-status reports while waiting; `tables.READOUT_TRAILING_REPORTS`; simulated |
| H1-b | ETS frames contain a few isolated out-of-place samples (H2: 10 us/div frequency reads up to 31.4 kHz instead of 29.9 kHz) | Not filtered in the driver; opt-in `analysis.despike()` exists (fixes ETS, corrupts undersampled realtime); T10/T11 decide whether to offer/apply it for ETS |
| H2-a (F7) | **Resolved**: `live_level()` hung on HW because `0x0D` starts an endless ~1 ms status stream and the reply loop never checked its deadline | Driver never sends `0x0D`; free-run frame capture instead; every worker wait loop has a worker-clock deadline AND a clock-independent bound; `tests/device/test_h2_streaming.py` |
| R1 | A report with first byte 0x64 is misread as status | FrameAssembler counts reports; it skips only *exact* `64 C8 mm` before the first data report |
| R2 | Two processes open the same hidraw node | OwnerLock (3.4) |
| R3 | hidapi backend differences (Linux libusb vs hidraw, Windows report-ID byte) | All contained in `HidTransport`, which has mocked tests |
| R4 | GUI thread blocked by I/O | Only the worker does I/O; the GUI gets queued signals |
| R5 | MCP payload bloat | Caps, 4-significant-digit rounding, minmax decimation, ids + resources for full data |
| R6 | Timing: polling at 20 ms intervals may miss frames at slow timebases | Not a correctness issue (timeouts scale with `Timebase`: trigger deadline >= 1.5 x frame duration + `timeout_s`) |
