"""OwnerLock: inter-process device ownership + owner discovery (architecture.md 3.4)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

import edu09.device.lock as lock_mod
from edu09.device.lock import (
    ENV_LOCK_DIR,
    OWNER_FILENAME,
    OwnerInfo,
    OwnerLock,
    default_lock_dir,
    read_owner,
)
from edu09.errors import DeviceBusy

WAIT_S = 30.0  # subprocess start-up (imports numpy) can be slow on CI

CHILD = r"""
import sys
from edu09.device.lock import OwnerLock
lock = OwnerLock("gui", mcp_url="http://127.0.0.1:8765/mcp", lock_dir=sys.argv[1])
lock.acquire()
print("ready", flush=True)
sys.stdin.readline()
lock.release()
"""


def wait_until(pred: Callable[[], bool], timeout: float = WAIT_S, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.01)


@pytest.fixture
def child(tmp_path: Path) -> Iterator[subprocess.Popen[str]]:
    proc = subprocess.Popen(
        [sys.executable, "-c", CHILD, str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:

        def owned() -> bool:
            if proc.poll() is not None:
                raise AssertionError(f"child exited early with {proc.returncode}")
            o = read_owner(tmp_path)
            return o is not None and o.pid == proc.pid

        wait_until(owned, what="child to own the lock")
        yield proc
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(WAIT_S)
        for f in (proc.stdin, proc.stdout):
            if f is not None:
                f.close()


def test_acquire_release_and_owner_file(tmp_path: Path) -> None:
    lock = OwnerLock("mcp", lock_dir=tmp_path)
    assert not lock.is_held and read_owner(tmp_path) is None
    lock.acquire()
    lock.acquire()  # idempotent
    assert lock.is_held
    doc = json.loads((tmp_path / OWNER_FILENAME).read_text())
    assert doc["pid"] == os.getpid() and doc["kind"] == "mcp" and doc["mcp_url"] is None
    assert doc["started"]
    lock.set_mcp_url("http://127.0.0.1:9000/mcp")
    owner = read_owner(tmp_path)
    assert owner == lock.info() and owner is not None
    assert owner.mcp_url == "http://127.0.0.1:9000/mcp"
    assert "mcp process" in owner.describe() and "9000" in owner.describe()
    lock.release()
    lock.release()  # idempotent
    assert not lock.is_held and read_owner(tmp_path) is None
    assert "free" in repr(lock)


def test_second_lock_in_same_process_is_busy(tmp_path: Path) -> None:
    with OwnerLock("gui", lock_dir=tmp_path, mcp_url="http://x/mcp"):
        other = OwnerLock("mcp", lock_dir=tmp_path)
        with pytest.raises(DeviceBusy) as ei:
            other.acquire()
        assert ei.value.owner == OwnerInfo(
            pid=os.getpid(), kind="gui", mcp_url="http://x/mcp", started=ei.value.owner.started
        )
        assert "gui process" in str(ei.value)
        assert not other.is_held
    with OwnerLock(lock_dir=tmp_path) as again:
        assert again.is_held


def test_cross_process_busy_then_free(tmp_path: Path, child: subprocess.Popen[str]) -> None:
    mine = OwnerLock("mcp", lock_dir=tmp_path)
    with pytest.raises(DeviceBusy) as ei:
        mine.acquire()
    owner = ei.value.owner
    assert isinstance(owner, OwnerInfo)
    assert owner.pid == child.pid and owner.kind == "gui"
    assert owner.mcp_url == "http://127.0.0.1:8765/mcp"
    assert child.stdin is not None
    child.stdin.write("\n")
    child.stdin.flush()
    assert child.wait(WAIT_S) == 0
    mine.acquire()
    try:
        assert read_owner(tmp_path) == mine.info()
    finally:
        mine.release()


def test_killed_owner_releases_lock(tmp_path: Path, child: subprocess.Popen[str]) -> None:
    with pytest.raises(DeviceBusy):
        OwnerLock(lock_dir=tmp_path).acquire()
    child.kill()
    child.wait(WAIT_S)
    stale = read_owner(tmp_path)
    assert stale is not None and stale.pid == child.pid  # stale file left behind
    with OwnerLock("mcp", lock_dir=tmp_path) as lock:  # the OS released the lock
        assert read_owner(tmp_path) == lock.info()


def test_corrupt_or_missing_owner_file(tmp_path: Path) -> None:
    (tmp_path / OWNER_FILENAME).write_text("{not json")
    assert read_owner(tmp_path) is None
    (tmp_path / OWNER_FILENAME).write_text('["list"]')
    info = read_owner(tmp_path)
    assert info is not None and info.pid is None and info.kind == "unknown"
    weird = OwnerInfo.from_dict({"pid": True, "kind": 3, "mcp_url": "", "started": 5})
    assert weird == OwnerInfo(pid=None, kind="unknown")
    with OwnerLock("gui", lock_dir=tmp_path):
        (tmp_path / OWNER_FILENAME).unlink()  # owner file vanished: busy without details
        with pytest.raises(DeviceBusy) as ei:
            OwnerLock(lock_dir=tmp_path).acquire()
        assert ei.value.owner is None and "another process" in str(ei.value)


def test_lock_dir_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_LOCK_DIR, str(tmp_path / "rt"))
    assert default_lock_dir() == tmp_path / "rt"
    with OwnerLock() as lock:
        assert lock.lock_path.parent == tmp_path / "rt"
        assert read_owner() == lock.info()
    monkeypatch.delenv(ENV_LOCK_DIR)
    monkeypatch.setattr(lock_mod.platformdirs, "user_runtime_dir", lambda *a, **k: str(tmp_path))
    monkeypatch.setattr(lock_mod.platformdirs, "user_cache_dir", lambda *a, **k: str(tmp_path))
    assert default_lock_dir() == tmp_path


def test_partial_owner_file_reads_as_none(tmp_path: Path) -> None:
    (tmp_path / OWNER_FILENAME).write_text('{"pid": 12')  # writer died mid-way (non-atomic FS)
    assert read_owner(tmp_path) is None
    (tmp_path / OWNER_FILENAME).write_bytes(b"\xff\xfe")
    assert read_owner(tmp_path) is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX runtime dir")
def test_lock_dir_falls_back_to_private_temp_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ENV_LOCK_DIR, raising=False)
    blocker = tmp_path / "file"
    blocker.write_text("")  # a file where the runtime dir's parent should be: mkdir fails
    monkeypatch.setattr(
        lock_mod.platformdirs, "user_runtime_dir", lambda *a, **k: str(blocker / "edu09")
    )
    monkeypatch.setattr(lock_mod.tempfile, "tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    d = default_lock_dir()
    assert d == tmp_path / "tmp" / f"edu09-{os.getuid()}"
    assert d.is_dir() and d.stat().st_mode & 0o777 == 0o700
    with OwnerLock("mcp") as lock:
        assert lock.lock_dir == d and read_owner() == lock.info()
    d.chmod(0o777)  # not private (e.g. pre-created by someone else): refused
    with pytest.raises(PermissionError):
        default_lock_dir()


def test_owner_write_retries_permission_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = os.replace
    fails = [2]

    def flaky(src: str, dst: str) -> None:
        if fails[0] > 0:
            fails[0] -= 1
            raise PermissionError("in use")  # Windows: a reader has owner.json open
        real(src, dst)

    monkeypatch.setattr(lock_mod.os, "replace", flaky)
    monkeypatch.setattr(lock_mod, "REPLACE_RETRY_SLEEP_S", 0.0)
    with OwnerLock("gui", lock_dir=tmp_path) as lock:
        assert fails == [0] and read_owner(tmp_path) == lock.info()
        fails[0] = 99
        with pytest.raises(PermissionError):
            lock.set_mcp_url("http://127.0.0.1:1/mcp")
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
