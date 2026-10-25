import edu09
from edu09 import errors


def test_version() -> None:
    assert isinstance(edu09.__version__, str)


def test_error_hierarchy() -> None:
    names = [
        "DeviceNotFound",
        "DeviceAccessError",
        "DeviceBusy",
        "DeviceDisconnected",
        "ProtocolError",
        "AcquisitionTimeout",
        "Cancelled",
        "CalibrationError",
        "UnsafeOperation",
    ]
    for name in names:
        cls = getattr(errors, name)
        assert issubclass(cls, errors.Edu09Error)
        assert getattr(edu09, name) is cls
    assert issubclass(errors.Edu09Error, Exception)


def test_model_reexports() -> None:
    for name in (
        "Settings", "ChannelSettings", "TriggerSettings", "Waveform", "VoltsPerDiv",
        "Timebase", "Coupling", "TriggerEdge", "TriggerMode",
    ):  # fmt: skip
        assert hasattr(edu09, name)
