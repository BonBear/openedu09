"""Device core: acquisition worker, thread-safe Scope API, owner lock, calibration, auto set."""

from edu09.device.autoset import AutoSetResult, auto_set
from edu09.device.calibration import measure_calibration
from edu09.device.lock import OwnerInfo, OwnerLock, default_lock_dir, read_owner
from edu09.device.scope import Scope, ScopeStatus
from edu09.device.worker import (
    AcquisitionWorker,
    ConnectionEvent,
    DeviceIO,
    ErrorEvent,
    Event,
    SettingsEvent,
    StateEvent,
    WorkerState,
    decode_frame,
    frame_duration_s,
)

__all__ = [
    "AcquisitionWorker",
    "AutoSetResult",
    "ConnectionEvent",
    "DeviceIO",
    "ErrorEvent",
    "Event",
    "OwnerInfo",
    "OwnerLock",
    "Scope",
    "ScopeStatus",
    "SettingsEvent",
    "StateEvent",
    "WorkerState",
    "auto_set",
    "decode_frame",
    "default_lock_dir",
    "frame_duration_s",
    "measure_calibration",
    "read_owner",
]
