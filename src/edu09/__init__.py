"""Driver library, MCP server and GUI for the Velleman EDU09 USB oscilloscope."""

from edu09.device.scope import Scope
from edu09.errors import (
    AcquisitionTimeout,
    CalibrationError,
    Cancelled,
    DeviceAccessError,
    DeviceBusy,
    DeviceDisconnected,
    DeviceNotFound,
    Edu09Error,
    JobAborted,
    ProtocolError,
    UnsafeOperation,
)
from edu09.model import (
    ChannelSettings,
    Coupling,
    Settings,
    Timebase,
    TriggerEdge,
    TriggerMode,
    TriggerSettings,
    VoltsPerDiv,
    Waveform,
)

__version__ = "0.1.0"

__all__ = [
    "AcquisitionTimeout",
    "CalibrationError",
    "Cancelled",
    "ChannelSettings",
    "Coupling",
    "DeviceAccessError",
    "DeviceBusy",
    "DeviceDisconnected",
    "DeviceNotFound",
    "Edu09Error",
    "JobAborted",
    "ProtocolError",
    "Scope",
    "Settings",
    "Timebase",
    "TriggerEdge",
    "TriggerMode",
    "TriggerSettings",
    "UnsafeOperation",
    "VoltsPerDiv",
    "Waveform",
    "__version__",
]
