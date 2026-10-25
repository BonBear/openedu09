"""Every protocol magic number for the EDU09, in one place.

Data only: ints, dicts, frozensets and frozen dataclasses. Nothing here imports ``edu09.model``;
tables are keyed by the model enums' *values* (volts/div as ``float``, seconds/div as ``float``),
for example ``GAIN_CODES_CH1[0.5] == 0x01`` and ``TIMEBASES[2e-3].hw_code == 0x08``.

Source: ``docs/protocol.md`` (static reverse engineering of EDU09.exe v1.06). Unless a comment says
"HW-verified", every value is **unverified on hardware**. Open questions are tagged ``Q1``..``Q10``
(protocol.md section 9 / architecture.md section 9). Correcting a layout after hardware probing
(task H1) should normally touch only this file plus test fixtures.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

# --------------------------------------------------------------------------------------------------
# USB identity (observed on real hardware, protocol.md 1.2)
# --------------------------------------------------------------------------------------------------
USB_VID: Final = 0x10CF
USB_PID: Final = 0xED90

# --------------------------------------------------------------------------------------------------
# Report framing (protocol.md 3, 4). Every transfer is one 64-byte report; no report IDs.
# --------------------------------------------------------------------------------------------------
REPORT_SIZE: Final = 64
"""Payload size of every IN and OUT report (excluding the hidapi report-ID byte)."""

FRAME_SIZE: Final = 1024
"""Samples (bytes) per waveform frame."""

REPORTS_PER_FRAME: Final = FRAME_SIZE // REPORT_SIZE  # 16

# --------------------------------------------------------------------------------------------------
# Opcodes (payload byte 0). Meanings are inferred unless noted (Q6).
# --------------------------------------------------------------------------------------------------
OP_STOP: Final = 0x08  # stop / abort. HW-verified: idle -> no reply; armed -> one all-zero report
OP_RESET: Final = 0x09  # reset acquisition state
OP_READOUT: Final = 0x0A  # start sending the captured 1024-byte frame (16 reports); early use: Q7
OP_ARM: Final = 0x0B  # arm. HW-verified: device then streams status by itself (Q1 answered)
OP_UNKNOWN_0C: Final = 0x0C  # never sent by EDU09.exe; meaning unknown (Q6). Driver never emits it.
# HW-verified (H2): NOT live samples. While armed it switches the device to a fast status stream
# (one status report per ~1 ms until 0x08); idle: no reply. Harmless, but the driver never sends it.
OP_LIVE: Final = 0x0D
OP_CONFIG: Final = 0x0E  # 10-byte SET CONFIGURATION. HW-verified: no reply. While armed: Q7
OP_VERSION: Final = 0x0F  # HW-verified: reply = ASCII "1.01" + CR (0x0D), then garbage
OP_RELEASE: Final = 0x14  # sent by EDU09.exe on close (release / power-down?); reply unknown (Q6)

# --------------------------------------------------------------------------------------------------
# Opcode safety policy (architecture.md 5, 7.1)
# --------------------------------------------------------------------------------------------------
DRIVER_OPCODES: Final[frozenset[int]] = frozenset(
    {OP_STOP, OP_RESET, OP_READOUT, OP_ARM, OP_LIVE, OP_CONFIG, OP_VERSION}
)
"""Opcodes the transport gate lets through. 0x14 is gated by SEND_RELEASE_ON_CLOSE; 0x0C is never
sent. 0x0D stays allowed (harmless, HW-verified H2; the probe sends it under ``--allow-acquire``),
but the device driver itself never emits it."""

SEND_RELEASE_ON_CLOSE: Final = False
"""Send OP_RELEASE (0x14) when closing the Scope. Off until probed on hardware (Q6)."""

OpcodeClass = Literal["read", "acquire", "unknown"]

OPCODE_CLASS: Final[dict[int, OpcodeClass]] = {
    OP_VERSION: "read",
    OP_STOP: "acquire",
    OP_RESET: "acquire",
    OP_READOUT: "acquire",
    OP_ARM: "acquire",
    OP_LIVE: "acquire",  # fast status stream while armed (H2); needs --allow-acquire in the probe
    OP_CONFIG: "acquire",
    OP_UNKNOWN_0C: "unknown",
    OP_RELEASE: "unknown",
}
"""Probe safety classes. Opcodes absent from this dict are also "unknown"."""

# --------------------------------------------------------------------------------------------------
# SET CONFIGURATION (0x0E) layout (protocol.md 3.2). Payload bytes 0..9, zero padding to 64.
# --------------------------------------------------------------------------------------------------
CONFIG_HEADER: Final = bytes((OP_CONFIG, 0x80, 0x07))
"""Bytes 0..2: opcode, constant 0x80 (sub-command?), 0x07 (count of config bytes, inferred)."""

CONFIG_LENGTH: Final = 10
"""Meaningful bytes in a config payload (header + 7 config bytes)."""

# Byte indices inside the config payload.
CFG_IDX_CH1: Final = 3  # CH1 gain bits | coupling bits
CFG_IDX_CH2: Final = 4  # CH2 gain bits | coupling bits (Q9: probably ignored by EDU09)
CFG_IDX_OFS1: Final = 5  # CH1 offset DAC
CFG_IDX_OFS2: Final = 6  # CH2 offset DAC (Q9)
CFG_IDX_TRIG: Final = 7  # trigger level byte
CFG_IDX_TB: Final = 8  # hardware timebase code (Table B)
CFG_IDX_FLAGS: Final = 9  # flag bits (Table C)

# Table A: volts/div -> gain bits (protocol.md 3.2). Keyed by VoltsPerDiv value.
GAIN_CODES_CH1: Final[dict[float, int]] = {
    0.1: 0x08,
    0.2: 0x00,
    0.5: 0x01,
    1.0: 0x0A,
    2.0: 0x02,
    5.0: 0x03,
}
GAIN_CODES_CH2: Final[dict[float, int]] = {  # PCSU200 leftover; EDU09 is single-channel (Q9)
    0.1: 0x22,
    0.2: 0x02,
    0.5: 0x24,
    1.0: 0x04,
    2.0: 0x28,
    5.0: 0x08,
}

FULL_SCALE_DIVISIONS: Final = 8
"""Vertical full scale in divisions (DSOLink export: 0.1 V/div -> 800 mV)."""

COUNTS_PER_DIV: Final = 32
"""ADC counts per vertical division (256 counts / 8 div). Inferred (protocol.md 5.2)."""

FULL_SCALE_MV: Final[dict[float, int]] = {
    v: round(v * 1000 * FULL_SCALE_DIVISIONS) for v in GAIN_CODES_CH1
}

# Coupling bits.
CH1_AC_BIT: Final = 0x04  # byte 3 |= 0x04 for CH1 AC; DC = bit clear; GND has no HW bit (software)
CH2_DC_BIT: Final = 0x01  # byte 4 |= 0x01 for CH2 DC
CH2_GND_BIT: Final = 0x10  # byte 4 |= 0x10 for CH2 GND

# Offset DAC (protocol.md 5.3).
OFFSET_DAC_DEFAULT: Final = 0x80
OFFSET_DAC_MIN: Final = 0x00
OFFSET_DAC_MAX: Final = 0xFF
CAL_BASE_OFS_MIN: Final = 0x5A  # calibration fails unless 0x5A < base_ofs < 0xA6 (exclusive bounds)
CAL_BASE_OFS_MAX: Final = 0xA6
RANGE_CORR_VDIVS: Final[frozenset[float]] = frozenset({0.1, 1.0})
"""V/div settings that get a calibrated range correction (efb378); all others use 0."""

OFFSET_DAC_SETTLE_S: Final = 0.02
"""HW-verified (2026-10-24): the offset DAC / front end needs ~10-20 ms to settle after a config
whose offset byte changed. Without a wait the first frame is off by up to ~8 counts at its start
(1 V/div, 200 us/div: first 100 samples mean 118.7 vs settled 127); the next frame (>= ~30 ms
later) is settled. The worker waits this long after such a ``0x0E`` before ``0x0B``."""

# Typical calibration of a real unit (HW-measured 2026-10-24, open input, 3 consistent runs of the
# driver's calibration). The DAC slope is ~1.03 ad counts per DAC step, NOT the 2 of the
# original's demo mode (protocol.md 5.3). ``units.Calibration.typical()`` uses these values as the
# fallback for hardware without a calibration file.
TYPICAL_BASE_OFS: Final = 0x86
TYPICAL_RANGE_CORR: Final[dict[float, int]] = {1.0: -1, 0.1: -1}
TYPICAL_GND_K: Final = 1.03
TYPICAL_GND_B: Final = -9.8
TYPICAL_RANGE_GAIN: Final[dict[float, float]] = {
    0.1: 0.987,
    0.2: 0.975,
    0.5: 0.974,
    1.0: 0.996,
    2.0: 0.981,
    5.0: 0.979,
}
"""Per-range gain correction ``V_true / V_read`` measured on ONE unit (2026-10-25: DC reference vs
multimeter, 5 ms/div, levels 2.4-6 div above ground). The uncorrected scope reads 0.4-2.6 % high,
i.e. the true counts per division is ``COUNTS_PER_DIV / gain`` (protocol.md 11.3)."""
RANGE_GAIN_MIN: Final = 0.8
RANGE_GAIN_MAX: Final = 1.25
"""Accepted gain corrections are ``RANGE_GAIN_MIN < gain < RANGE_GAIN_MAX`` (exclusive)."""

# Neutral CH2 bytes (architecture.md 4.1: CH2 not modelled -> DC, 2 V/div, offset 0x80). Q9.
CH2_NEUTRAL_CFG: Final = GAIN_CODES_CH2[2.0] | CH2_DC_BIT  # 0x29
CH2_NEUTRAL_OFS: Final = OFFSET_DAC_DEFAULT

# Trigger level byte (payload byte 7). EDU09.exe sends 255 - TrackBar3.Position, where the trackbar
# runs 0 (top of screen) .. 255. The byte is the raw ADC count at the trigger level, in the same
# domain as the samples. HW-verified (H1): byte 165 on a 0..4.16 V square (raw 132..197) triggers
# mid-edge at ~2.1 V and frames are phase-stable, so the byte is NOT inverted (Q3/Q5 answered).
TRIGGER_BYTE_INVERTED: Final = False
# EDU09.exe default: 255 - TrackBar3.Position(128) = 127; with the uncalibrated model
# GndFit(k=2, b=-128) 0 V -> raw 127.
TRIGGER_BYTE_DEFAULT: Final = 127  # 0x7F

# Table C: FLAGS byte (payload byte 9). Bits 4..7 must be 0 (garbage in the original).
FLAG_TRIG_SRC_CH2: Final = 0x01  # 1 = trigger on CH2; EDU09 always uses CH1 (0)
FLAG_TRIG_ENABLE: Final = 0x02  # 1 = trigger on, 0 = free run
FLAG_EDGE_FALLING: Final = 0x04  # HW-verified (Q5): 0 = rising "Up", 1 = falling "Dn"
FLAG_DIGITAL: Final = 0x08  # "digital level grabber" mode; not exposed (Q5)
FLAGS_VALID_MASK: Final = 0x0F

# ADC polarity: True if a larger raw count means a MORE NEGATIVE input voltage.
# HW-verified False (Q3, H1): at 2 V/div a 0..4.16 V square reads raw ~132 (0 V) and ~197 (4.16 V);
# 65 counts for 4.16 V (66.6 predicted by 32 counts/div).
ADC_INVERTED: Final = False

ADC_MIN: Final = 0
ADC_MAX: Final = 255

# --------------------------------------------------------------------------------------------------
# Timebase table (protocol.md 3.2 Table B). Keyed by Timebase value (seconds/div).
# --------------------------------------------------------------------------------------------------
MODE_REALTIME: Final = 1
MODE_ETS8: Final = 2
MODE_ETS16: Final = 3
MODE_ETS16_ZOOM: Final = 4


@dataclass(frozen=True, slots=True)
class TimebaseEntry:
    """One time/div setting.

    ``sample_rate_hz`` is the rate exported by EDU09.exe to DSOLink; for ETS rows it is the
    effective (post de-interleave) rate. Q10, HW-verified with a ~30 kHz square (H1): 200 us/div
    (156.25 kHz), 50 us (625 kHz), 20 us and 10 us (both 1.5625 MHz) measure 29.9-30.05 kHz.
    ``mode`` is the acquisition mode byte the status report must carry (HW-verified, modes 1-4).
    ``sw_zoom`` is the extra software zoom EDU09.exe applied for display (the hardware setting is
    shared with a slower timebase); the driver ignores it,
    ``dt`` is always ``1 / sample_rate_hz``.
    """

    s_per_div: float
    hw_code: int
    sample_rate_hz: float
    mode: int
    sw_zoom: int = 1

    @property
    def dt(self) -> float:
        """Seconds per sample."""
        return 1.0 / self.sample_rate_hz

    @property
    def samples_per_div(self) -> float:
        """Samples per screen division at this setting (``sample_rate_hz * s_per_div``)."""
        return self.sample_rate_hz * self.s_per_div

    @property
    def divisions_per_frame(self) -> float:
        """Divisions covered by one 1024-sample frame (about 65.5 for ``sw_zoom=2`` rows)."""
        return FRAME_SIZE / self.samples_per_div

    @property
    def is_ets(self) -> bool:
        return self.mode != MODE_REALTIME


TIMEBASES: Final[dict[float, TimebaseEntry]] = {
    e.s_per_div: e
    for e in (
        TimebaseEntry(500e-3, 0x01, 62.5, MODE_REALTIME),  # exported as 62 (int); 31.25 S/div rule
        TimebaseEntry(200e-3, 0x02, 156.25, MODE_REALTIME),  # exported as 156
        TimebaseEntry(100e-3, 0x03, 312.5, MODE_REALTIME),  # exported as 312
        TimebaseEntry(50e-3, 0x04, 625.0, MODE_REALTIME),
        TimebaseEntry(20e-3, 0x05, 1_562.5, MODE_REALTIME),  # exported as 1562
        TimebaseEntry(10e-3, 0x06, 3_125.0, MODE_REALTIME),
        TimebaseEntry(5e-3, 0x07, 6_250.0, MODE_REALTIME),
        TimebaseEntry(2e-3, 0x08, 15_625.0, MODE_REALTIME),  # EDU09.exe start-up default
        TimebaseEntry(1e-3, 0x09, 31_250.0, MODE_REALTIME),
        TimebaseEntry(500e-6, 0x0A, 62_500.0, MODE_REALTIME),
        TimebaseEntry(200e-6, 0x0B, 156_250.0, MODE_REALTIME),  # HW-verified rate
        TimebaseEntry(100e-6, 0x0B, 156_250.0, MODE_REALTIME, sw_zoom=2),  # same HW as 200 us
        TimebaseEntry(50e-6, 0x0C, 625_000.0, MODE_ETS8),  # HW-verified rate
        TimebaseEntry(20e-6, 0x0D, 1_562_500.0, MODE_ETS16),  # HW-verified rate
        # HW-verified: same sample spacing as 20 us (1.5625 MHz) despite own HW code 0x10 / mode 4;
        # the zoom is display-only.
        TimebaseEntry(10e-6, 0x10, 1_562_500.0, MODE_ETS16_ZOOM, sw_zoom=2),
    )
}
"""Realtime rates follow the exact 31.25 samples/div rule (DSOLink truncates to int; Q10)."""

DEFAULT_TIMEBASE: Final = 2e-3
DEFAULT_VOLTS_PER_DIV: Final = 2.0

SAMPLES_PER_DIV: Final = 31.25
"""Nominal non-zoomed samples/div; sw_zoom=2 rows differ, see ``TimebaseEntry.samples_per_div``."""
DIVISIONS_PER_FRAME: Final = FRAME_SIZE / SAMPLES_PER_DIV  # 32.768
"""Nominal (non-zoomed) divisions per frame; use ``TimebaseEntry.divisions_per_frame``."""

# ETS: mode byte -> number of interleaved sub-records (protocol.md 4.3).
ETS_SUBRECORDS: Final[dict[int, int]] = {
    MODE_REALTIME: 1,
    MODE_ETS8: 8,
    MODE_ETS16: 16,
    MODE_ETS16_ZOOM: 16,
}
VALID_MODES: Final[frozenset[int]] = frozenset(ETS_SUBRECORDS)

# --------------------------------------------------------------------------------------------------
# Status report (protocol.md 4.1)
# --------------------------------------------------------------------------------------------------
# HW-verified (H1, 2026-10-24, firmware 1.01): COMPLETE = ``64 C8 mm`` followed by 61 zero bytes
# (mode 1 seen at 2 ms and 500 us/div). WAITING is NOT ``64 xx mm`` but an all-zero 64-byte report.
STATUS_MARKER: Final = 0x64  # byte 0
STATUS_COMPLETE: Final = 0xC8  # byte 1 when the capture is complete (HW-verified)
STATUS_IDX_MARKER: Final = 0
STATUS_IDX_FLAG: Final = 1
STATUS_IDX_MODE: Final = 2

WAITING_REPORT: Final = bytes(REPORT_SIZE)
"""HW-verified "still waiting" status report while armed: 64 zero bytes (not ``64 00 mm``).

Ambiguous on its own (a data report of raw 0 samples looks the same), so it is only a status
report *before* the 0x0A read-out; ``FrameAssembler`` never skips it (R1)."""

StatusPollStrategy = Literal["passive", "resend_arm"]
STATUS_POLL: Final[StatusPollStrategy] = "passive"
"""Q1 answered (HW-verified): after 0x0B the device streams status reports by itself ("passive").

The host never prompts; it just reads. While waiting for a trigger it sends ``WAITING_REPORT``
every ``STATUS_INTERVAL_S``; when the capture completes it sends ``64 C8 mm`` immediately (a free
run completes about one frame time after the arm and sent no waiting reports before that).
``"resend_arm"`` is kept only as a simulator option."""

STATUS_INTERVAL_S: Final = 0.033
"""HW-verified: interval of the streamed waiting reports while armed in realtime modes (~33 ms,
about 30 Hz; the first one ~33 ms after 0x0B)."""

STATUS_INTERVAL_ETS_S: Final = 0.002
"""HW-observed: in ETS modes (2..4) waiting reports come much faster, every ~1-4 ms (mean ~2 ms)
until the capture completes (presumably one per ETS pass; Inferred)."""

READOUT_LEADING_STATUS: Final = 2
"""``64 C8 mm`` reports seen after 0x0A before the 16 data reports (HW: 1..3; 2 at 2 ms/div,
usually 1 -- the count depends on how long the host waits before 0x0A, so ``FrameAssembler``
skips any number up to its limit). The simulator default."""

READOUT_TRAILING_REPORTS: Final = 1
"""HW-observed: after the 16 frame reports the device sends ONE more data-like report ~1 ms later
(not part of the frame: with ETS it was byte-identical across 10/20/50 us captures). A host that
does not drain it sees a stray non-status report after the next 0x0B."""

# --------------------------------------------------------------------------------------------------
# Trigger position and timing
# --------------------------------------------------------------------------------------------------
TRIGGER_INDEX_BY_MODE: Final[dict[int, int]] = {
    MODE_REALTIME: 255,
    MODE_ETS8: 248,
    MODE_ETS16: 253,
    MODE_ETS16_ZOOM: 253,
}
"""HW-verified (Q4, H1, +-1): index of the first sample past the trigger level on the triggering
edge in the time-ordered (de-interleaved) 1024-sample frame. Not the help file's 258. Realtime was
measured at 200 us/div (only ~5 samples per period, hence +-1); ETS 50 us -> 248 (247..248),
20 us -> 253, 10 us -> 253 (orchestrator's reading: 252)."""

TRIGGER_INDEX: Final = TRIGGER_INDEX_BY_MODE[MODE_REALTIME]
"""Realtime trigger index (255). Use ``TRIGGER_INDEX_BY_MODE`` / ``units.trigger_index(tb)``."""

POLL_READ_TIMEOUT_S: Final = 0.02
FRAME_READ_TIMEOUT_S: Final = 0.2

# --------------------------------------------------------------------------------------------------
# Version reply
# --------------------------------------------------------------------------------------------------
VERSION_LENGTH: Final = 4  # HW-verified: bytes 0..3 ASCII, e.g. b"1.01"
VERSION_TERMINATOR: Final = 0x0D  # HW-verified: CR right after the version; the rest is garbage
VERSION_MAX_LENGTH: Final = 16  # longest CR/NUL-terminated version string accepted

# --------------------------------------------------------------------------------------------------
# 0x0D "live" (H2, HW-verified on firmware 1.01): a fast status stream, never ADC samples
# --------------------------------------------------------------------------------------------------
FAST_STATUS_INTERVAL_S: Final = 0.001
"""HW-verified (H2): after ``0x09, 0x0B, 0x0D`` the device sends one status report every ~1 ms
(``WAITING_REPORT`` while waiting, ``64 C8 mm`` repeatedly once complete; bytes 3..63 always zero)
until ``0x08``, which yields one final report. A second 0x0D changes nothing; 0x0D while idle gets
no reply. The original's calibration (``255 - mean(report)``) would read these status reports on
this firmware, so the driver measures levels from free-run frames instead."""

POST_STOP_REPORT_DELAY_S: Final = (0.0008, 0.0082)
"""HW-observed (H2): the one report after 0x08 while armed (all-zero, or ``64 C8 mm`` if the capture
had completed) arrives 0.8..8.2 ms after the stop; well inside the worker's ~43 ms window."""
