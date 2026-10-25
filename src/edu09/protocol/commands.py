"""Host -> device command encoders.

Every encoder returns exactly ``tables.REPORT_SIZE`` (64) bytes: the payload, zero-padded,
**without** the hidapi report-ID byte (the transport adds it). See protocol.md section 3.
"""

from __future__ import annotations

from dataclasses import dataclass

from edu09.errors import UnsafeOperation
from edu09.protocol import tables


@dataclass(frozen=True, slots=True)
class HwConfig:
    """Raw byte fields of the 0x0E SET CONFIGURATION payload (bytes 3..9).

    All fields are already-encoded bytes (0..255); higher layers
    (``units.settings_to_hwconfig``) own the Settings -> bytes mapping. Defaults reproduce the
    EDU09.exe start-up state: CH1 2 V/div DC, neutral CH2, offsets 0x80, trigger byte 127 (0x7F),
    2 ms/div, trigger on, rising edge.

    - ``gain1``: byte 3, CH1 gain bits (``tables.GAIN_CODES_CH1``) | ``tables.CH1_AC_BIT``.
    - ``gain2``: byte 4, CH2 gain/coupling bits (Q9; neutral by default).
    - ``ofs1`` / ``ofs2``: bytes 5/6, offset DAC values.
    - ``trig``: byte 7, trigger level byte.
    - ``tb``: byte 8, hardware timebase code (``tables.TIMEBASES[...].hw_code``).
    - ``flags``: byte 9, ``tables.FLAG_*`` bits; bits 4..7 must be zero.
    """

    gain1: int = tables.GAIN_CODES_CH1[tables.DEFAULT_VOLTS_PER_DIV]
    gain2: int = tables.CH2_NEUTRAL_CFG
    ofs1: int = tables.OFFSET_DAC_DEFAULT
    ofs2: int = tables.CH2_NEUTRAL_OFS
    trig: int = tables.TRIGGER_BYTE_DEFAULT
    tb: int = tables.TIMEBASES[tables.DEFAULT_TIMEBASE].hw_code
    flags: int = tables.FLAG_TRIG_ENABLE

    def __post_init__(self) -> None:
        for name in ("gain1", "gain2", "ofs1", "ofs2", "trig", "tb", "flags"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"HwConfig.{name} must be int, got {type(value).__name__}")
            if not 0 <= value <= 0xFF:
                raise ValueError(f"HwConfig.{name} must be in 0..255, got {value}")
        if self.flags & ~tables.FLAGS_VALID_MASK:
            raise ValueError(
                f"HwConfig.flags has undefined bits set: 0x{self.flags:02X} "
                f"(allowed mask 0x{tables.FLAGS_VALID_MASK:02X})"
            )

    def to_bytes(self) -> bytes:
        """The 7 config bytes (payload bytes 3..9)."""
        return bytes((self.gain1, self.gain2, self.ofs1, self.ofs2, self.trig, self.tb, self.flags))


def _pad(payload: bytes) -> bytes:
    if len(payload) > tables.REPORT_SIZE:
        raise ValueError(f"payload too long: {len(payload)} > {tables.REPORT_SIZE}")
    return payload + bytes(tables.REPORT_SIZE - len(payload))


def encode_simple(op: int) -> bytes:
    """Encode a 1-byte command. Only opcodes in ``tables.DRIVER_OPCODES`` (except 0x0E) are allowed.

    Raises ``UnsafeOperation`` for any other opcode (0x0C, 0x14, unknown) and for 0x0E, which needs
    its config bytes (use ``encode_config``).
    """
    if op == tables.OP_CONFIG:
        raise UnsafeOperation("opcode 0x0E needs config bytes; use encode_config()")
    if op not in tables.DRIVER_OPCODES:
        raise UnsafeOperation(f"opcode 0x{op & 0xFF:02X} is not in the driver's allowed set")
    return _pad(bytes((op,)))


def encode_version_query() -> bytes:
    """0x0F: firmware version query (HW-verified)."""
    return encode_simple(tables.OP_VERSION)


def encode_reset() -> bytes:
    """0x09: reset acquisition state."""
    return encode_simple(tables.OP_RESET)


def encode_arm() -> bytes:
    """0x0B: arm / start capture."""
    return encode_simple(tables.OP_ARM)


def encode_readout() -> bytes:
    """0x0A: request read-out of the 1024-sample frame."""
    return encode_simple(tables.OP_READOUT)


def encode_stop() -> bytes:
    """0x08: stop / abort acquisition."""
    return encode_simple(tables.OP_STOP)


def encode_live() -> bytes:
    """0x0D: HW-verified (H2) to start a ~1 ms *status* stream while armed (no reply when idle).

    It never returns ADC samples on firmware 1.01. Kept for the probe; the driver does not use it.
    """
    return encode_simple(tables.OP_LIVE)


def encode_release() -> bytes:
    """0x14: release on close. Gated by ``tables.SEND_RELEASE_ON_CLOSE`` (Q6, unprobed).

    Raises ``UnsafeOperation`` while the gate is off.
    """
    if not tables.SEND_RELEASE_ON_CLOSE:
        raise UnsafeOperation("opcode 0x14 is disabled (tables.SEND_RELEASE_ON_CLOSE is False)")
    return _pad(bytes((tables.OP_RELEASE,)))


def encode_config(cfg: HwConfig) -> bytes:
    """0x0E SET CONFIGURATION: ``0E 80 07`` + 7 config bytes, zero-padded to 64 bytes."""
    return _pad(tables.CONFIG_HEADER + cfg.to_bytes())


def decode_config(payload: bytes) -> HwConfig:
    """Inverse of ``encode_config`` (for the simulator, replay and tests).

    Raises ``ValueError`` if the payload is not a 0x0E config report.
    """
    if len(payload) < tables.CONFIG_LENGTH:
        raise ValueError(f"config payload too short: {len(payload)} bytes")
    header = payload[: len(tables.CONFIG_HEADER)]
    if header != tables.CONFIG_HEADER:
        raise ValueError(f"not a config payload: header {header.hex(' ')}")
    b = payload
    return HwConfig(
        gain1=b[tables.CFG_IDX_CH1],
        gain2=b[tables.CFG_IDX_CH2],
        ofs1=b[tables.CFG_IDX_OFS1],
        ofs2=b[tables.CFG_IDX_OFS2],
        trig=b[tables.CFG_IDX_TRIG],
        tb=b[tables.CFG_IDX_TB],
        flags=b[tables.CFG_IDX_FLAGS],
    )
