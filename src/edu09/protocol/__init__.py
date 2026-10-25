"""Pure EDU09 wire protocol: tables, command encoders, report parsers and ETS reordering.

This package imports only the standard library, ``edu09.errors`` and its own
modules. It does no I/O.
"""

from edu09.protocol import tables
from edu09.protocol.commands import (
    HwConfig,
    decode_config,
    encode_arm,
    encode_config,
    encode_live,
    encode_readout,
    encode_release,
    encode_reset,
    encode_simple,
    encode_stop,
    encode_version_query,
)
from edu09.protocol.ets import deinterleave, interleave
from edu09.protocol.reports import (
    FrameAssembler,
    Status,
    is_complete_status,
    is_waiting_report,
    parse_status,
    parse_version,
)

__all__ = [
    "FrameAssembler",
    "HwConfig",
    "Status",
    "decode_config",
    "deinterleave",
    "encode_arm",
    "encode_config",
    "encode_live",
    "encode_readout",
    "encode_release",
    "encode_reset",
    "encode_simple",
    "encode_stop",
    "encode_version_query",
    "interleave",
    "is_complete_status",
    "is_waiting_report",
    "parse_status",
    "parse_version",
    "tables",
]
