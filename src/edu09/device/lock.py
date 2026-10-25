"""Inter-process device ownership (architecture.md 3.4).

hidraw (Linux) and hidapi (Windows) let several processes open the same EDU09, and interleaved
polling would silently corrupt frames. So exactly one process owns the device: it holds a
``filelock`` on ``<lock dir>/device.lock`` and publishes a sidecar ``owner.json``::

    {"pid": 1234, "kind": "gui", "mcp_url": "http://127.0.0.1:8765/mcp", "started": "<ISO-8601>"}

Another process that fails to take the lock gets ``DeviceBusy`` whose ``owner`` attribute is the
parsed :class:`OwnerInfo` (used by the MCP stdio bridge to find the owner's HTTP endpoint).

The OS releases the lock when the owner dies (``flock`` / ``msvcrt.locking``), so a stale
``owner.json`` is simply overwritten by the next owner. Within one process, two ``OwnerLock``
instances also exclude each other (each opens its own file descriptor).

Lock directory: ``$EDU09_RUNTIME_DIR`` if set, else ``platformdirs.user_runtime_dir("edu09")``
(Windows: the user cache dir). If that directory cannot be created (e.g. no ``/run/user/<uid>``
in a container or cron job), a per-user directory under the temp dir is used instead
(``<tmp>/edu09-<user>``, mode 0700).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import platformdirs
from filelock import FileLock, Timeout

from edu09.errors import DeviceBusy

__all__ = [
    "ENV_LOCK_DIR",
    "LOCK_FILENAME",
    "OWNER_FILENAME",
    "OwnerInfo",
    "OwnerLock",
    "default_lock_dir",
    "read_owner",
]

log = logging.getLogger(__name__)

APP_NAME = "edu09"
LOCK_FILENAME = "device.lock"
OWNER_FILENAME = "owner.json"
ENV_LOCK_DIR = "EDU09_RUNTIME_DIR"
REPLACE_RETRIES = 5
"""Windows: ``os.replace`` onto ``owner.json`` fails while another process has it open."""
REPLACE_RETRY_SLEEP_S = 0.02


def _user_tag() -> str:
    getuid = getattr(os, "getuid", None)
    if getuid is not None:
        return str(getuid())
    try:
        import getpass

        return getpass.getuser()
    except Exception:  # no usable user name: still per-process-tree stable enough
        return "user"


def _fallback_lock_dir() -> Path:
    """``<tmp>/edu09-<user>``, created 0700. In a shared temp dir another user could create it
    first (and plant an ``owner.json`` with a foreign MCP URL), so its owner is checked."""
    path = Path(tempfile.gettempdir()) / f"{APP_NAME}-{_user_tag()}"
    path.mkdir(mode=0o700, exist_ok=True)
    getuid = getattr(os, "getuid", None)
    if getuid is not None:
        st = path.stat()
        if st.st_uid != getuid() or st.st_mode & 0o022:
            raise PermissionError(f"refusing to use lock directory {path}: not private to us")
    return path


def default_lock_dir() -> Path:
    """Directory holding ``device.lock`` and ``owner.json`` (see the module docstring).

    Creates the platformdirs directory if needed; falls back to ``<tmp>/edu09-<user>`` when
    that is impossible.
    """
    env = os.environ.get(ENV_LOCK_DIR)
    if env:
        return Path(env)
    if sys.platform == "win32":
        primary = Path(platformdirs.user_cache_dir(APP_NAME, appauthor=False))
    else:
        primary = Path(platformdirs.user_runtime_dir(APP_NAME, appauthor=False))
    try:
        primary.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        fallback = _fallback_lock_dir()
        log.debug("cannot use %s (%s); using %s", primary, exc, fallback)
        return fallback
    return primary


@dataclass(frozen=True)
class OwnerInfo:
    """Who owns the device. ``kind`` is "gui", "mcp" or "other"; ``pid`` None if unknown."""

    pid: int | None
    kind: str = "other"
    mcp_url: str | None = None
    started: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "kind": self.kind,
            "mcp_url": self.mcp_url,
            "started": self.started,
        }

    @classmethod
    def from_dict(cls, d: Any) -> OwnerInfo:
        """Lenient parse (bad/missing fields become None / "other")."""
        if not isinstance(d, dict):
            return cls(pid=None, kind="unknown")
        pid = d.get("pid")
        url = d.get("mcp_url")
        started = d.get("started")
        kind = d.get("kind")
        return cls(
            pid=pid if isinstance(pid, int) and not isinstance(pid, bool) else None,
            kind=kind if isinstance(kind, str) else "unknown",
            mcp_url=url if isinstance(url, str) and url else None,
            started=started if isinstance(started, str) else None,
        )

    def describe(self) -> str:
        who = f"{self.kind} process" + (f" (pid {self.pid})" if self.pid is not None else "")
        if self.mcp_url:
            who += f", MCP at {self.mcp_url}"
        return who


def read_owner(lock_dir: str | os.PathLike[str] | None = None) -> OwnerInfo | None:
    """Parse ``owner.json`` (None if missing or unreadable). Does not check the lock itself."""
    path = (Path(lock_dir) if lock_dir is not None else default_lock_dir()) / OWNER_FILENAME
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.debug("unreadable %s: %s", path, exc)
        return None
    return OwnerInfo.from_dict(doc)


def _atomic_write_json(path: Path, doc: dict[str, Any]) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
            fh.flush()
            os.fsync(fh.fileno())
        for attempt in range(REPLACE_RETRIES):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:  # Windows: a reader has owner.json open right now
                if attempt == REPLACE_RETRIES - 1:
                    raise
                time.sleep(REPLACE_RETRY_SLEEP_S)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


class OwnerLock:
    """Exclusive, inter-process ownership of the EDU09 (non-blocking).

    Usage::

        lock = OwnerLock(kind="gui")
        lock.acquire()               # raises DeviceBusy(owner=OwnerInfo) if held elsewhere
        lock.set_mcp_url("http://127.0.0.1:8765/mcp")
        ...
        lock.release()

    Thread-safe; ``release`` may be called from any thread and is idempotent.
    """

    def __init__(
        self,
        kind: str = "other",
        *,
        mcp_url: str | None = None,
        lock_dir: str | os.PathLike[str] | None = None,
    ) -> None:
        self.kind = kind
        self._mcp_url = mcp_url
        self.lock_dir = Path(lock_dir) if lock_dir is not None else default_lock_dir()
        self._file_lock: FileLock | None = None
        self._started: str | None = None
        self._mutex = threading.Lock()

    @property
    def lock_path(self) -> Path:
        return self.lock_dir / LOCK_FILENAME

    @property
    def owner_path(self) -> Path:
        return self.lock_dir / OWNER_FILENAME

    @property
    def is_held(self) -> bool:
        return self._file_lock is not None

    @property
    def mcp_url(self) -> str | None:
        return self._mcp_url

    def info(self) -> OwnerInfo:
        """Our own owner record (what ``owner.json`` holds while we own the device)."""
        return OwnerInfo(
            pid=os.getpid(), kind=self.kind, mcp_url=self._mcp_url, started=self._started
        )

    def acquire(self) -> None:
        """Take ownership or raise ``DeviceBusy`` (with ``owner``) immediately."""
        with self._mutex:
            if self._file_lock is not None:
                return
            self.lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            fl = FileLock(str(self.lock_path), timeout=0, thread_local=False)
            try:
                fl.acquire(timeout=0)
            except Timeout:
                owner = read_owner(self.lock_dir)
                desc = owner.describe() if owner is not None else "another process"
                raise DeviceBusy(f"EDU09 is in use by {desc}", owner=owner) from None
            self._file_lock = fl
            self._started = datetime.now(UTC).isoformat()
            try:
                self._write_owner()
            except OSError:
                fl.release()
                self._file_lock = None
                raise

    def set_mcp_url(self, url: str | None) -> None:
        """Publish (or clear) the URL of our in-process MCP HTTP server in ``owner.json``."""
        with self._mutex:
            self._mcp_url = url
            if self._file_lock is not None:
                self._write_owner()

    def release(self) -> None:
        with self._mutex:
            fl, self._file_lock = self._file_lock, None
            if fl is None:
                return
            current = read_owner(self.lock_dir)
            if current is not None and current.pid == os.getpid():
                with contextlib.suppress(OSError):
                    self.owner_path.unlink()
            with contextlib.suppress(Exception):
                fl.release()

    def _write_owner(self) -> None:
        _atomic_write_json(self.owner_path, self.info().to_dict())

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()

    def __repr__(self) -> str:
        state = "held" if self.is_held else "free"
        return f"OwnerLock(kind={self.kind!r}, {state}, dir={str(self.lock_dir)!r})"
