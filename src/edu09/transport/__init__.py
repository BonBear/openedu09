"""Transport layer: 64-byte report pipes (HID, simulator, replay)."""

from edu09.transport.base import USB_PID, USB_VID, Transport, TransportInfo
from edu09.transport.discovery import DeviceInfo, find_device, list_devices
from edu09.transport.hid_transport import HidTransport
from edu09.transport.replay import RecordingTransport, ReplayTransport
from edu09.transport.sim import SignalSpec, SimTransport, SimulatedDevice, VirtualClock

__all__ = [
    "USB_PID",
    "USB_VID",
    "DeviceInfo",
    "HidTransport",
    "RecordingTransport",
    "ReplayTransport",
    "SignalSpec",
    "SimTransport",
    "SimulatedDevice",
    "Transport",
    "TransportInfo",
    "VirtualClock",
    "find_device",
    "list_devices",
]
