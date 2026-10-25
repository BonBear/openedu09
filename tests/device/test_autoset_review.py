"""Reviewer tests for T9 auto set: error paths with a minimal fake Scope."""

from __future__ import annotations

from typing import Any

import pytest

from edu09.device import auto_set
from edu09.errors import DeviceDisconnected
from edu09.model import Settings


class _FakeScope:
    """acquire() fails; set_settings() succeeds for auto_set's own calls, then the restore of the
    original settings times out (worker hung)."""

    def __init__(self, running: bool) -> None:
        self.settings = Settings()
        self.running = running
        self.calls: list[str] = []

    def stop(self) -> None:
        self.calls.append("stop")
        self.running = False

    def run(self) -> None:
        self.calls.append("run")
        raise TimeoutError("the acquisition worker did not answer within 10.0 s")

    def set_settings(self, settings: Settings, *, source: str | None = None) -> Settings:
        self.calls.append("set")
        if settings == self.settings:  # the restore
            raise TimeoutError("the acquisition worker did not answer within 10.0 s")
        return settings

    def acquire(self, **_: Any) -> Any:
        raise DeviceDisconnected("unplugged")


@pytest.mark.parametrize("running", [False, True])
def test_original_error_wins_over_restore_timeouts(running: bool) -> None:
    sc = _FakeScope(running)
    with pytest.raises(DeviceDisconnected, match="unplugged"):
        auto_set(sc, timeout_s=1.0)  # type: ignore[arg-type]
    assert sc.calls[-2:] == (["set", "run"] if running else ["set", "set"])
