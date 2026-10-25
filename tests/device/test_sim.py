"""Simulator (T6): device-side protocol model + round trips through the real host-side codecs."""

from __future__ import annotations

import numpy as np
import numpy.typing as npt
import pytest

from edu09.analysis import measure
from edu09.errors import DeviceDisconnected, DeviceNotFound, UnsafeOperation
from edu09.model import (
    ChannelSettings,
    Coupling,
    Settings,
    Timebase,
    TriggerEdge,
    TriggerMode,
    TriggerSettings,
    VoltsPerDiv,
)
from edu09.protocol import tables
from edu09.protocol.commands import (
    HwConfig,
    encode_arm,
    encode_config,
    encode_live,
    encode_readout,
    encode_reset,
    encode_stop,
    encode_version_query,
)
from edu09.protocol.ets import deinterleave
from edu09.protocol.reports import FrameAssembler, parse_status, parse_version
from edu09.transport import SignalSpec, SimTransport, SimulatedDevice, Transport, VirtualClock
from edu09.units import (
    Calibration,
    counts_to_volts,
    expected_acq_mode,
    sample_interval,
    settings_to_hwconfig,
    trigger_byte,
    trigger_index,
)

CAL = Calibration()
TI = tables.TRIGGER_INDEX
STRATEGIES = ["passive", "resend_arm"]


def _settings(
    vdiv: float = 1.0,
    tb: Timebase = Timebase.MS2,
    *,
    coupling: Coupling = Coupling.DC,
    position: float = 0.0,
    mode: TriggerMode = TriggerMode.NORMAL,
    edge: TriggerEdge = TriggerEdge.RISING,
    level: float = 0.0,
) -> Settings:
    return Settings(
        ch1=ChannelSettings(VoltsPerDiv.parse(vdiv), coupling, position),
        timebase=tb,
        trigger=TriggerSettings(mode=mode, edge=edge, level_v=level),
    )


def _open(spec: str, strategy: str = "passive", seed: int = 0) -> SimTransport:
    t = SimTransport.virtual(spec, seed=seed, status_poll=strategy)  # type: ignore[arg-type]
    t.open()
    return t


def _wait_complete(t: SimTransport, mode: int, max_reads: int = 20_000) -> int:
    """Poll for ``64 C8 mm`` using the device's strategy; returns the number of status reads."""
    for n in range(1, max_reads + 1):
        r = t.read(tables.POLL_READ_TIMEOUT_S)
        st = parse_status(r) if r is not None else None
        if st is not None and st.complete and st.mode == mode:
            return n
        if t.device.status_poll == "resend_arm":
            t.write(encode_arm())
    raise AssertionError("capture never completed")


def _read_frame(t: SimTransport, mode: int) -> tuple[bytes, FrameAssembler]:
    asm = FrameAssembler(expected_mode=mode)
    for _ in range(100):
        if asm.feed(t.read(tables.FRAME_READ_TIMEOUT_S)):
            return asm.frame, asm
    raise AssertionError(f"frame incomplete: {asm.reports_received} reports")


def _acquire(
    t: SimTransport, s: Settings, cal: Calibration = CAL
) -> tuple[npt.NDArray[np.uint8], npt.NDArray[np.float64], bytes]:
    """Full host-side round trip; returns (time-ordered raw, volts, wire-order frame)."""
    t.write(encode_config(settings_to_hwconfig(s, cal)))
    t.write(encode_reset())
    t.write(encode_arm())
    mode = expected_acq_mode(s.timebase)
    _wait_complete(t, mode)
    t.write(encode_readout())
    wire, _ = _read_frame(t, mode)
    raw = np.frombuffer(deinterleave(mode, wire), dtype=np.uint8)
    return raw, counts_to_volts(raw, s.ch1, cal), wire


# --------------------------------------------------------------------------------------------------
# Transport basics
# --------------------------------------------------------------------------------------------------
def test_implements_transport_protocol() -> None:
    t = SimTransport.virtual()
    assert isinstance(t, Transport)
    assert t.info.kind == "sim"
    assert t.info.path == "sim:sine:1000hz:2vpp"


def test_requires_open_and_exact_payload() -> None:
    t = SimTransport.virtual()
    with pytest.raises(DeviceDisconnected):
        t.read(0.01)
    with pytest.raises(DeviceDisconnected):
        t.write(encode_version_query())
    t.open()
    with pytest.raises(ValueError):
        t.write(b"\x0f")
    t.close()
    t.close()  # idempotent
    assert not t.is_open


def test_version_reply() -> None:
    t = _open("dc:0v")
    t.write(encode_version_query())
    r = t.read(0.2)
    assert r is not None and len(r) == 64
    assert r[:5] == b"1.01\r"  # HW-verified: CR right after the version
    assert parse_version(r) == "1.01"
    assert t.read(0.01) is None  # nothing else queued


def test_config_has_no_reply() -> None:
    t = _open("dc:0v")
    t.write(encode_config(settings_to_hwconfig(_settings(), CAL)))
    assert t.read(0.5) is None


def test_idle_read_times_out_and_advances_virtual_clock() -> None:
    t = _open("dc:0v")
    clock = t.device.clock
    assert isinstance(clock, VirtualClock)
    before = clock()
    assert t.read(0.05) is None
    assert clock() == pytest.approx(before + 0.05)


def test_real_clock_default_constructs() -> None:
    t = SimTransport("sine:1khz:1vpp")
    t.open()
    assert t.read(0.001) is None
    t.close()


# --------------------------------------------------------------------------------------------------
# Opcode gate and ignored commands
# --------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("op", [tables.OP_UNKNOWN_0C, tables.OP_RELEASE, 0x00, 0x42])
def test_opcode_gate_blocks_unknown(op: int) -> None:
    t = _open("dc:0v")
    with pytest.raises(UnsafeOperation):
        t.write(bytes([op]) + bytes(63))
    assert t.device.received == []


def test_unknown_opcode_let_through_is_ignored() -> None:
    t = SimTransport.virtual("dc:0v", allow_unknown_opcodes=True)
    t.open()
    t.write(bytes([0x0C]) + bytes(63))
    t.write(bytes([0x14]) + bytes(63))
    assert t.read(0.01) is None
    assert t.device.received == [0x0C, 0x14]
    assert len(t.device.ignored) == 2


def test_malformed_config_ignored() -> None:
    t = _open("dc:0v")
    t.write(bytes([0x0E, 0x00, 0x07]) + bytes(61))
    assert t.device.config == HwConfig()
    assert "malformed config" in t.device.ignored[-1]


def test_config_decoded() -> None:
    t = _open("dc:0v")
    s = _settings(0.5, Timebase.US20, coupling=Coupling.AC)
    cfg = settings_to_hwconfig(s, CAL)
    t.write(encode_config(cfg))
    assert t.device.config == cfg
    assert t.device.volts_per_div == 0.5
    assert t.device.mode == tables.MODE_ETS16


def test_unknown_gain_and_timebase_codes_keep_previous() -> None:
    t = _open("dc:0v")
    t.write(encode_config(HwConfig(gain1=0x7F & ~0x04, tb=0x0F)))
    assert t.device.volts_per_div == tables.DEFAULT_VOLTS_PER_DIV
    assert t.device.timebase.s_per_div == tables.DEFAULT_TIMEBASE


# --------------------------------------------------------------------------------------------------
# Status / arm / read-out state machine
# --------------------------------------------------------------------------------------------------
def _arm_at(t: SimTransport, s: Settings) -> float:
    t.write(encode_config(settings_to_hwconfig(s, CAL)))
    t.write(encode_reset())
    t.write(encode_arm())
    return t.device.clock()


def test_passive_free_run_completes_after_frame_time_without_waiting_reports() -> None:
    """HW (freerun_2ms fixture): no report until ``64 C8 01`` about one frame time after 0x0B."""
    t = _open("sine:1khz:2vpp", "passive")
    t.write(encode_config(settings_to_hwconfig(_settings(), CAL)))
    assert t.read(0.01) is None  # idle: nothing unsolicited (HW-verified behaviour)
    t0 = _arm_at(t, _settings(mode=TriggerMode.NONE))
    assert t.read(0.02) is None  # nothing during the 65.5 ms frame
    r = t.read(1.0)
    assert r == bytes([0x64, 0xC8, 1]) + bytes(61)  # HW: zero padded
    frame_s = tables.FRAME_SIZE * tables.TIMEBASES[2e-3].dt
    assert t.device.clock() - t0 == pytest.approx(frame_s + 0.001, abs=1e-6)
    assert t.read(0.1) is None  # completion is announced once
    t.write(encode_readout())
    _, asm = _read_frame(t, 1)
    assert asm.skipped_status == tables.READOUT_LEADING_STATUS == 2


def test_passive_trigger_wait_streams_zero_reports_every_33ms() -> None:
    """HW (trigger_timeout_2ms fixture): all-zero report every ~33 ms while waiting."""
    t = _open("dc:0.5v", "passive")  # never crosses the 0 V level
    t0 = _arm_at(t, _settings(1.0, level=0.0))
    times = []
    for _ in range(10):
        r = t.read(0.05)
        assert r == tables.WAITING_REPORT
        times.append(t.device.clock() - t0)
    gaps = np.diff([0.0, *times])
    # each read returns at the tick + one 1 ms report interval
    assert gaps[0] == pytest.approx(tables.STATUS_INTERVAL_S + 0.001)
    assert np.allclose(gaps[1:], tables.STATUS_INTERVAL_S)
    assert t.read(0.02) is None  # 20 ms < 33 ms: times out between reports


def test_passive_backlog_is_bounded() -> None:
    t = _open("dc:0.5v", "passive")
    _arm_at(t, _settings(1.0, level=0.0))
    t.device.clock.sleep(10.0)  # type: ignore[attr-defined]
    n = 0
    while t.read(0.0) is not None:
        n += 1
    assert 32 <= n <= 33  # bounded buffer (+1 tick that falls due while draining)


def test_resend_arm_needs_prompting() -> None:
    t = _open("sine:1khz:2vpp", "resend_arm")
    t.write(encode_config(settings_to_hwconfig(_settings(), CAL)))
    t.write(encode_reset())
    t.write(encode_arm())
    r = t.read(0.02)
    assert r == tables.WAITING_REPORT
    assert t.read(0.1) is None  # nothing streamed
    t.device.clock.sleep(1.0)  # type: ignore[attr-defined]
    t.write(encode_arm())  # re-arm while armed = status poll, capture not restarted
    r = t.read(0.02)
    assert r is not None and r[:3] == bytes([0x64, 0xC8, 1])


def test_readout_before_complete_is_ignored() -> None:
    t = _open("sine:1khz:2vpp")
    t.write(encode_config(settings_to_hwconfig(_settings(), CAL)))
    t.write(encode_reset())
    t.write(encode_arm())
    t.write(encode_readout())
    assert "0x0A before capture complete" in t.device.ignored[-1]
    assert t.device.pending_reports == 0


def test_readout_skips_leading_status_and_returns_16_reports() -> None:
    t = _open("sine:1khz:2vpp")
    s = _settings()
    t.write(encode_config(settings_to_hwconfig(s, CAL)))
    t.write(encode_reset())
    t.write(encode_arm())
    _wait_complete(t, 1)
    t.write(encode_readout())
    # HW: two leading statuses, 16 frame reports, one trailing non-frame report
    assert t.device.pending_reports == 2 + tables.REPORTS_PER_FRAME + 1
    _, asm = _read_frame(t, 1)
    assert asm.skipped_status == 2
    trailing = t.read(0.01)
    assert trailing is not None and parse_status(trailing) is None
    assert t.read(0.01) is None  # back to idle after the frame
    assert t.device.state == "idle"
    assert t.device.frames_sent == 1


def test_stop_aborts_capture() -> None:
    """HW: 0x08 while armed -> one all-zero report, then silence; idle -> no reply."""
    t = _open("sine:1khz:2vpp")
    t.write(encode_config(settings_to_hwconfig(_settings(), CAL)))
    t.write(encode_reset())
    t.write(encode_arm())
    t.write(encode_stop())
    assert t.device.state == "idle"
    assert t.read(0.01) == tables.WAITING_REPORT
    assert t.read(0.5) is None
    t.write(encode_stop())  # idle
    assert t.read(0.5) is None


def test_config_while_armed_restarts_capture() -> None:
    t = _open("sine:1khz:2vpp", "resend_arm")
    t.write(encode_config(settings_to_hwconfig(_settings(), CAL)))
    t.write(encode_reset())
    t.write(encode_arm())
    t.read(0.01)
    t.device.clock.sleep(1.0)  # type: ignore[attr-defined]
    t.write(encode_config(settings_to_hwconfig(_settings(tb=Timebase.MS1), CAL)))
    t.write(encode_arm())
    r = t.read(0.01)
    assert r == tables.WAITING_REPORT  # restarted: waiting again
    assert t.device.state == "armed"


def test_slow_status_wait_polls() -> None:
    t = _open("sine:1khz:2vpp")
    t.device.wait_polls = 50
    t0 = _arm_at(t, _settings())
    waiting = 0
    for _ in range(200):
        r = t.read(tables.POLL_READ_TIMEOUT_S)
        if r == tables.WAITING_REPORT:
            waiting += 1
        elif r is not None:
            break
    assert r is not None and r[:3] == bytes([0x64, 0xC8, 1])
    assert waiting == 50
    assert t.device.clock() - t0 >= 50 * tables.STATUS_INTERVAL_S


# --------------------------------------------------------------------------------------------------
# 0x0D (H2, HW-verified): fast status stream, never live samples
# --------------------------------------------------------------------------------------------------
COMPLETE_1 = bytes([0x64, 0xC8, 1]) + bytes(61)


def test_live_opcode_when_idle_gets_no_reply() -> None:
    """HW (h2_live_0d fixture): ``0x09, 0x0D`` -> nothing."""
    t = _open("dc:1v")
    t.write(encode_reset())
    t.write(encode_live())
    assert t.read(0.2) is None
    assert t.device.pending_reports == 0


def test_live_opcode_while_armed_streams_status_every_ms_until_stop() -> None:
    """HW: after ``0x09, 0x0B, 0x0D`` one report per ~1 ms: zeros while waiting, then ``64 C8 01``
    repeatedly once complete (never samples); ``0x08`` ends it with one final report."""
    t = _open("dc:1v")
    t0 = _arm_at(t, _settings(1.0, Timebase.MS2, mode=TriggerMode.NONE))
    t.write(encode_live())
    frame_s = tables.FRAME_SIZE * tables.TIMEBASES[2e-3].dt  # 65.5 ms
    got: list[bytes] = []
    for _ in range(100):
        r = t.read(0.01)
        assert r is not None  # never a gap: one report per ms
        got.append(r)
    assert all(r == tables.WAITING_REPORT or r == COMPLETE_1 for r in got)
    n_wait = sum(r == tables.WAITING_REPORT for r in got)
    assert abs(n_wait - frame_s / tables.FAST_STATUS_INTERVAL_S) <= 2  # until complete (~65 ms)
    assert got[n_wait:] == [COMPLETE_1] * (100 - n_wait)  # complete repeats every ms
    assert t.device.clock() - t0 == pytest.approx(0.1, abs=0.002)  # one per ms
    t.write(encode_live())  # a second 0x0D changes nothing
    assert t.read(0.01) == COMPLETE_1
    t.write(encode_stop())
    assert t.read(0.01) == COMPLETE_1  # the final report after 0x08 (capture had completed)
    assert t.read(0.2) is None


def test_live_opcode_waiting_then_stop_gives_one_zero_report() -> None:
    t = _open("dc:1v")
    _arm_at(t, _settings(1.0, Timebase.MS2, mode=TriggerMode.NORMAL, level=3.0))  # never fires
    t.write(encode_live())
    assert [t.read(0.01) for _ in range(30)] == [tables.WAITING_REPORT] * 30
    t.write(encode_stop())
    assert t.read(0.01) == tables.WAITING_REPORT
    assert t.read(0.2) is None


def test_live_opcode_report_is_never_sample_data() -> None:
    t = _open("dc:1v")  # raw 159: any live sample would be non-zero
    _arm_at(t, _settings(1.0, Timebase.US200, mode=TriggerMode.NONE))
    t.write(encode_live())
    for _ in range(50):
        r = t.read(0.01)
        assert r is not None and not any(r[3:])


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_status_reports_are_zero_padded(strategy: str) -> None:
    t = _open("dc:0v", strategy)
    _arm_at(t, _settings(mode=TriggerMode.NONE))
    _wait_complete(t, 1)
    t.write(encode_readout())
    r = t.read(0.01)
    assert r == bytes([0x64, 0xC8, 1]) + bytes(61)


# --------------------------------------------------------------------------------------------------
# Round trips: config -> arm -> status -> read-out -> assembler -> ETS -> volts -> analysis
# --------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("strategy", STRATEGIES)
@pytest.mark.parametrize(
    ("tb", "freq", "vdiv"),
    [
        (Timebase.MS2, 1_000.0, 1.0),
        (Timebase.MS50, 10.0, 0.5),
        (Timebase.US200, 10_000.0, 2.0),
        (Timebase.US50, 20_000.0, 1.0),  # ETS x8
        (Timebase.US20, 50_000.0, 1.0),  # ETS x16
        (Timebase.US10, 100_000.0, 0.5),  # ETS x16 (zoom row)
    ],
)
def test_round_trip_recovers_sine(strategy: str, tb: Timebase, freq: float, vdiv: float) -> None:
    t = _open(f"sine:{freq}hz:2vpp", strategy)
    s = _settings(vdiv, tb)
    raw, volts, wire = _acquire(t, s)
    dt = sample_interval(tb)
    m = measure(volts, dt, ["frequency", "vpp", "mean"])
    assert m["frequency"].valid
    assert m["frequency"].value == pytest.approx(freq, rel=0.01)
    assert m["vpp"].value == pytest.approx(2.0, rel=0.03)
    assert m["mean"].value == pytest.approx(0.0, abs=0.05)
    # Trigger (rising through 0 V) sits at the trigger index.
    level = trigger_byte(s.trigger, s.ch1, CAL)
    ti = trigger_index(tb)  # HW-verified per mode (Q4)
    assert ti == tables.TRIGGER_INDEX_BY_MODE[expected_acq_mode(tb)]
    assert raw[ti - 1] <= level <= raw[ti]
    assert int(raw[ti + 2]) > int(raw[ti - 2])
    if tb.is_ets:
        assert wire != raw.tobytes()  # really interleaved on the wire


@pytest.mark.parametrize(
    ("edge", "level"), [(TriggerEdge.RISING, 0.5), (TriggerEdge.FALLING, -0.5)]
)
def test_trigger_level_and_edge(edge: TriggerEdge, level: float) -> None:
    t = _open("triangle:100hz:4vpp")  # ~0.05 V per sample
    s = _settings(1.0, edge=edge, level=level)
    _, volts, _ = _acquire(t, s)
    lsb = 1.0 / 32
    assert volts[TI] == pytest.approx(level, abs=2 * lsb)
    slope = volts[TI + 3] - volts[TI - 3]
    assert slope > 0 if edge is TriggerEdge.RISING else slope < 0


def test_repeated_frames_are_trigger_aligned() -> None:
    t = _open("square:330hz:3vpp:duty=30%", "passive")
    s = _settings(1.0)
    frames = []
    for _ in range(4):
        raw, volts, _ = _acquire(t, s)
        frames.append(raw)
        assert volts[TI] > 0 > volts[TI - 1]
        m = measure(volts, sample_interval(s.timebase), ["duty", "frequency"])
        assert m["duty"].value == pytest.approx(30.0, abs=2.0)
        assert m["frequency"].value == pytest.approx(330.0, rel=0.01)
    assert t.device.frames_sent == 4


def test_position_offset_round_trip() -> None:
    t = _open("sine:1khz:2vpp:offset=1v")
    s = _settings(1.0, position=-2.0, level=1.0)
    raw, volts, _ = _acquire(t, s)
    assert raw.min() > 0 and raw.max() < 255
    assert float(np.mean(volts)) == pytest.approx(1.0, abs=0.05)
    assert float(np.ptp(volts)) == pytest.approx(2.0, rel=0.03)


def test_ac_coupling_removes_dc() -> None:
    t = _open("sine:1khz:2vpp:offset=1.5v")
    ac = _settings(1.0, coupling=Coupling.AC)
    _, volts, _ = _acquire(t, ac)
    assert float(np.mean(volts)) == pytest.approx(0.0, abs=0.05)
    dc = _settings(1.0, level=1.5)
    _, volts, _ = _acquire(t, dc)
    assert float(np.mean(volts)) == pytest.approx(1.5, abs=0.05)


def test_clipping_saturates() -> None:
    t = _open("sine:1khz:5vpp")
    raw, _, _ = _acquire(t, _settings(0.1))
    assert raw.min() == 0 and raw.max() == 255


def test_free_run_completes_without_edge() -> None:
    t = _open("dc:0.5v")
    s = _settings(1.0, mode=TriggerMode.NONE)
    raw, volts, _ = _acquire(t, s)
    assert np.all(raw == 127 + 16)
    assert np.allclose(volts, 0.5)


def test_no_edge_never_triggers() -> None:
    t = _open("dc:0.5v")
    t.write(encode_config(settings_to_hwconfig(_settings(1.0, level=0.0), CAL)))
    t.write(encode_reset())
    t.write(encode_arm())
    for _ in range(200):
        r = t.read(0.05)
        assert r == tables.WAITING_REPORT


def test_never_trigger_knob() -> None:
    t = _open("sine:1khz:2vpp")
    t.device.never_trigger = True
    t.write(encode_config(settings_to_hwconfig(_settings(), CAL)))
    t.write(encode_reset())
    t.write(encode_arm())
    with pytest.raises(AssertionError, match="never completed"):
        _wait_complete(t, 1, max_reads=500)


def test_noise_signal_and_determinism() -> None:
    def grab(seed: int) -> npt.NDArray[np.uint8]:
        t = _open("sine:1khz:2vpp:noise=50mv", seed=seed)
        raw, _, _ = _acquire(t, _settings(1.0))
        return raw

    a, b, c = grab(1), grab(1), grab(2)
    assert np.array_equal(a, b)
    assert not np.array_equal(a, c)
    t = _open("noise:200mvrms")
    _, volts, _ = _acquire(t, _settings(0.5))
    assert float(np.std(volts)) == pytest.approx(0.2, rel=0.15)


def test_signal_change_is_live() -> None:
    t = _open("sine:1khz:2vpp")
    s = _settings(1.0)
    _acquire(t, s)
    t.device.signal = SignalSpec.parse("sine:2khz:1vpp")
    _, volts, _ = _acquire(t, s)
    m = measure(volts, sample_interval(s.timebase), ["frequency", "vpp"])
    assert m["frequency"].value == pytest.approx(2000.0, rel=0.01)
    assert m["vpp"].value == pytest.approx(1.0, rel=0.05)


# --------------------------------------------------------------------------------------------------
# Fault injection
# --------------------------------------------------------------------------------------------------
def test_unplug_mid_frame_and_replug() -> None:
    t = _open("sine:1khz:2vpp")
    s = _settings()
    t.write(encode_config(settings_to_hwconfig(s, CAL)))
    t.write(encode_reset())
    t.write(encode_arm())
    _wait_complete(t, 1)
    t.write(encode_readout())
    t.schedule_unplug(after_reads=5)
    for _ in range(5):
        assert t.read(0.2) is not None
    with pytest.raises(DeviceDisconnected):
        t.read(0.2)
    with pytest.raises(DeviceDisconnected):
        t.write(encode_version_query())
    with pytest.raises(DeviceNotFound):
        t.open()
    t.close()  # never raises
    t.replug()
    t.open()
    assert t.device.config == HwConfig()  # power-on state
    assert t.device.pending_reports == 0
    t.write(encode_version_query())
    r = t.read(0.2)
    assert r is not None and parse_version(r) == "1.01"
    _, volts, _ = _acquire(t, s)
    assert measure(volts, sample_interval(s.timebase), ["vpp"])["vpp"].value == pytest.approx(
        2.0, rel=0.03
    )


def test_immediate_unplug() -> None:
    t = _open("dc:0v")
    t.unplug()
    assert not t.plugged and not t.is_open
    with pytest.raises(DeviceDisconnected):
        t.read(0.01)


def test_dropped_report_leaves_frame_incomplete() -> None:
    t = _open("sine:1khz:2vpp")
    # The trailing report would otherwise fill the gap: counting alone cannot detect a lost report.
    t.device.trailing_reports = 0
    t.device.drop_frame_report = 7
    t.write(encode_config(settings_to_hwconfig(_settings(), CAL)))
    t.write(encode_reset())
    t.write(encode_arm())
    _wait_complete(t, 1)
    t.write(encode_readout())
    asm = FrameAssembler(expected_mode=1)
    for _ in range(40):
        asm.feed(t.read(0.2))
    assert not asm.done and asm.reports_received == 15
    assert t.device.drop_frame_report is None  # one-shot


def test_device_constructor_validation() -> None:
    with pytest.raises(ValueError):
        SimulatedDevice(status_poll="bogus")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        SimulatedDevice(trigger_index=1024)
    with pytest.raises(ValueError):
        SimulatedDevice(status_interval_s=0.0)


def test_custom_device_and_sleep_injection() -> None:
    clock = VirtualClock(100.0)
    slept: list[float] = []
    dev = SimulatedDevice("dc:0v", clock=clock, trigger_index=100)
    t = SimTransport(device=dev, sleep=slept.append)
    t.open()
    assert t.read(0.03) is None
    assert slept == [0.03]
    assert dev.now() == 0.0


# --------------------------------------------------------------------------------------------------
# SignalSpec
# --------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("sine:1khz:2vpp", SignalSpec("sine", 1000.0, 1.0)),
        ("SIN : 50Hz : 500mVpp", SignalSpec("sine", 50.0, 0.25)),
        ("square:50hz:5vpp:duty=25%:offset=2.5v", SignalSpec("square", 50.0, 2.5, 2.5, 0.25)),
        ("square:1khz:1vp:10%", SignalSpec("square", 1000.0, 1.0, duty=0.1)),
        ("tri:2mhz:1vpk", SignalSpec("triangle", 2e6, 1.0)),
        ("saw:f=1k:amp=0.3:phase=90deg", SignalSpec("sawtooth", 1000.0, 0.3, phase_deg=90.0)),
        ("dc:1.5v", SignalSpec("dc", offset_v=1.5)),
        ("dc:-200mv", SignalSpec("dc", offset_v=-0.2)),
        ("noise:100mvrms", SignalSpec("noise", amplitude_v=0.1)),
        ("sine:1khz:1vrms", SignalSpec("sine", 1000.0, 2**0.5)),
        ("sine:1khz:2vpp:noise=10mv", SignalSpec("sine", 1000.0, 1.0, noise_v=0.01)),
        ("sine:freq=440:vpp=1:dc=0.1:duty=0.4", SignalSpec("sine", 440.0, 0.5, 0.1, 0.4)),
        ("sine", SignalSpec()),
    ],
)
def test_signal_spec_parse(text: str, expected: SignalSpec) -> None:
    got = SignalSpec.parse(text)
    for name in ("kind", "frequency_hz", "amplitude_v", "offset_v", "duty", "phase_deg", "noise_v"):
        assert getattr(got, name) == pytest.approx(getattr(expected, name)), name


@pytest.mark.parametrize(
    "text",
    [
        "",
        "square2",
        "sine:1kq",
        "sine:abc",
        "sine:bogus=1",
        "sine:0hz",
        "sine:-1vpp",
        "square:duty=150%",
        "sine:f=1v",
        "sine:2uvpp",
    ],
)
def test_signal_spec_parse_errors(text: str) -> None:
    with pytest.raises(ValueError):
        SignalSpec.parse(text)


@pytest.mark.parametrize(
    "spec",
    [
        SignalSpec(),
        SignalSpec("square", 123.5, 0.75, -0.25, 0.3, 45.0, 0.02),
        SignalSpec("dc", offset_v=0.0),
        SignalSpec("noise", amplitude_v=0.2, offset_v=1.0),
        SignalSpec("sawtooth", 1e-3, 2.0),
    ],
)
def test_signal_spec_round_trip(spec: SignalSpec) -> None:
    assert SignalSpec.parse(spec.to_spec()) == spec
    assert str(spec) == spec.to_spec()


def test_signal_shapes() -> None:
    t = np.arange(10_000) / 10_000.0  # one period at 1 Hz
    sq = SignalSpec("square", 1.0, 1.0, duty=0.25).evaluate(t)
    assert float(np.mean(sq)) == pytest.approx(
        SignalSpec("square", 1.0, 1.0, duty=0.25).dc_component()
    )
    tri = SignalSpec("triangle", 1.0, 2.0).evaluate(t)
    assert tri[0] == pytest.approx(0.0) and tri.max() == pytest.approx(2.0)
    assert tri.min() == pytest.approx(-2.0, abs=1e-3)
    saw = SignalSpec("sawtooth", 1.0, 1.0).evaluate(t)
    assert saw[0] == pytest.approx(0.0) and saw[2499] > saw[0]
    sine = SignalSpec("sine", 1.0, 1.0, 0.5).evaluate(t)
    assert float(np.mean(sine)) == pytest.approx(0.5, abs=1e-6)
    dc = SignalSpec("dc", amplitude_v=3.0, offset_v=0.7).evaluate(t)
    assert np.all(dc == 0.7)
    assert SignalSpec("dc").vpp == 0.0 and SignalSpec(amplitude_v=1.5).vpp == 3.0
    assert np.all(SignalSpec("noise").evaluate(t) == 0.0)  # no rng -> no noise


def test_signal_spec_validation() -> None:
    with pytest.raises(ValueError):
        SignalSpec("wobble")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        SignalSpec(frequency_hz=float("nan"))
    with pytest.raises(ValueError):
        SignalSpec(duty=1.0)
    assert SignalSpec("SQ").kind == "square"  # type: ignore[arg-type]


def test_ets_waiting_reports_use_fast_interval() -> None:
    """HW (square_trig_50us/20us fixtures): ETS waiting reports every ~2 ms, not 33 ms."""
    t = _open("dc:0.5v")  # never crosses 0 V
    t0 = _arm_at(t, _settings(1.0, Timebase.US20, level=0.0))
    for _ in range(5):
        assert t.read(0.01) == tables.WAITING_REPORT
    per_report = tables.STATUS_INTERVAL_ETS_S
    assert t.device.clock() - t0 == pytest.approx(5 * per_report + 0.001)


def test_trigger_index_defaults_per_mode() -> None:
    dev = SimulatedDevice("dc:0v")
    assert dev.trigger_index is None
    for mode, ti in tables.TRIGGER_INDEX_BY_MODE.items():
        assert dev.trigger_index_for(mode) == ti
    assert SimulatedDevice("dc:0v", trigger_index=100).trigger_index_for(3) == 100
