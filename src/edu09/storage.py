"""Persistence helpers: calibration JSON, vendor ``EDU09.ini`` import, CSV export, waveform store.

Nothing here talks to the device and no threads are started. ``WaveformStore`` is thread-safe
(one ``threading.Lock``) because the worker callback stores waveforms while MCP/GUI threads read.
"""

from __future__ import annotations

import builtins
import configparser
import contextlib
import csv
import io
import json
import os
import tempfile
import threading
from collections import OrderedDict
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

import platformdirs

from .errors import CalibrationError, Edu09Error
from .model import Waveform
from .units import Calibration, GndFit

APP_NAME = "edu09"
CALIBRATION_FILENAME = "calibration.json"
CALIBRATION_FILE_VERSION = 1
DEFAULT_STORE_CAPACITY = 32  # architecture.md section 6: "store keeps the last 32"
INI_SECTION = "Calibration"  # protocol.md 5.4 step 5
CSV_COLUMNS = ("t_s", "volts", "raw")


# --------------------------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------------------------


def default_config_dir() -> Path:
    return Path(platformdirs.user_config_dir(APP_NAME, appauthor=False))


def default_data_dir() -> Path:
    return Path(platformdirs.user_data_dir(APP_NAME, appauthor=False))


def default_calibration_path() -> Path:
    return default_config_dir() / CALIBRATION_FILENAME


def default_export_dir() -> Path:
    return default_data_dir() / "exports"


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` via a temp file in the same directory + ``os.replace``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


# --------------------------------------------------------------------------------------------
# Calibration JSON
# --------------------------------------------------------------------------------------------


def save_calibration(cal: Calibration, path: str | os.PathLike[str] | None = None) -> Path:
    """Atomically write ``cal`` as JSON (``{"version": 1, "calibration": {...}}``)."""
    target = Path(path) if path is not None else default_calibration_path()
    doc = {"version": CALIBRATION_FILE_VERSION, "calibration": cal.to_dict()}
    _atomic_write_text(target, json.dumps(doc, indent=2, sort_keys=True) + "\n")
    return target


def load_calibration(path: str | os.PathLike[str] | None = None) -> Calibration:
    """Load a calibration file. A missing file gives the default ``Calibration()``.

    Raises ``CalibrationError`` for unreadable/invalid content. A bare ``to_dict()`` mapping
    (without the version wrapper) is accepted too.
    """
    target = Path(path) if path is not None else default_calibration_path()
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Calibration()
    except (OSError, UnicodeDecodeError) as exc:
        raise CalibrationError(f"cannot read calibration file {target}: {exc}") from exc
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise CalibrationError(f"calibration file {target} is not valid JSON: {exc}") from exc
    if isinstance(doc, Mapping) and "calibration" in doc:
        version = doc.get("version")
        if version != CALIBRATION_FILE_VERSION:
            raise CalibrationError(f"unsupported calibration file version {version!r}")
        doc = doc["calibration"]
    return Calibration.from_dict(doc)


def backup_calibration(path: str | os.PathLike[str] | None = None) -> Path | None:
    """Copy an existing calibration file to ``<stem>.<timestamp>.bak.json`` (None if absent)."""
    target = Path(path) if path is not None else default_calibration_path()
    if not target.is_file():
        return None
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    backup = target.with_name(f"{target.stem}.{stamp}.bak{target.suffix}")
    _atomic_write_text(backup, target.read_text(encoding="utf-8"))
    return backup


# --------------------------------------------------------------------------------------------
# Vendor EDU09.ini import (read-only)
# --------------------------------------------------------------------------------------------


def import_vendor_ini(path: str | os.PathLike[str]) -> Calibration:
    """Convert the vendor ``EDU09.ini`` ``[Calibration]`` section into a ``Calibration``.

    Known keys (protocol.md 5.3): ``Ch1_pos0..2`` (DAC) and ``Ch1_ad0..2`` (AD measured with
    ``ReadBytes`` = ``255 - mean``), fitted with ``GndFit.from_points``. The key names for
    ``base_ofs`` / ``range_corr`` are not known, so those keep their defaults. The file is
    never modified. Raises ``CalibrationError`` when missing or malformed.
    """
    src = Path(path)
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        # utf-8-sig: tolerate a BOM (Notepad re-save); the vendor app writes ANSI/ASCII.
        with src.open(encoding="utf-8-sig", errors="replace") as fh:
            parser.read_file(fh)
    except OSError as exc:
        raise CalibrationError(f"cannot read {src}: {exc}") from exc
    except configparser.Error as exc:
        raise CalibrationError(f"{src} is not a valid INI file: {exc}") from exc
    if not parser.has_section(INI_SECTION):
        raise CalibrationError(f"{src} has no [{INI_SECTION}] section")
    sec = parser[INI_SECTION]
    points: list[tuple[float, float]] = []
    for i in range(3):
        try:
            # Delphi FloatToStr uses the locale decimal separator (e.g. "127,5" on sv/fi).
            pos = float(sec[f"Ch1_pos{i}"].strip().replace(",", "."))
            ad = float(sec[f"Ch1_ad{i}"].strip().replace(",", "."))
        except KeyError as exc:
            raise CalibrationError(f"{src}: missing key {exc.args[0]} in [{INI_SECTION}]") from None
        except ValueError as exc:
            raise CalibrationError(f"{src}: non-numeric calibration value: {exc}") from None
        points.append((pos, ad))
    return Calibration(gnd_fit=GndFit.from_points(points), source="ini-import")


# --------------------------------------------------------------------------------------------
# CSV export
# --------------------------------------------------------------------------------------------


def _fmt_float(x: float) -> str:
    return repr(float(x))


def waveform_to_csv(wf: Waveform) -> str:
    """CSV text: ``#`` comment header (metadata + settings JSON), then ``t_s,volts,raw`` rows."""
    out = io.StringIO()
    meta: dict[str, Any] = {
        "waveform_id": wf.waveform_id,
        "timestamp": wf.timestamp.isoformat(),
        "sample_rate_hz": wf.sample_rate_hz,
        "dt_s": wf.dt,
        "samples": wf.n,
        "trigger_index": wf.trigger_index,
        "triggered": wf.triggered,
        "clipped": wf.clipped,
        "calibration_id": wf.calibration_id,
    }
    out.write("# edu09 waveform export\n")
    for key, val in meta.items():
        out.write(f"# {key}: {json.dumps(val)}\n")
    out.write(f"# settings: {json.dumps(wf.settings.to_dict(), sort_keys=True)}\n")
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    t = wf.t()
    for i in range(wf.n):
        writer.writerow((_fmt_float(t[i]), _fmt_float(wf.volts[i]), int(wf.raw[i])))
    return out.getvalue()


def resolve_export_path(filename: str, export_dir: str | os.PathLike[str] | None = None) -> Path:
    """Map a user-supplied file name into ``export_dir``; reject absolute paths and ``..``."""
    if not filename or "\x00" in filename:
        raise ValueError("invalid export filename")
    if (
        PurePosixPath(filename).is_absolute()
        or PureWindowsPath(filename).is_absolute()
        or PureWindowsPath(filename).drive
        or ".." in PurePosixPath(filename.replace("\\", "/")).parts
    ):
        raise ValueError(
            f"export filename must be relative and inside the export dir: {filename!r}"
        )
    base = Path(export_dir) if export_dir is not None else default_export_dir()
    target = base / filename
    if not target.resolve().is_relative_to(base.resolve()):
        raise ValueError(f"export filename escapes the export dir: {filename!r}")
    return target


def export_csv(wf: Waveform, path: str | os.PathLike[str]) -> Path:
    """Write ``wf`` to ``path`` as CSV (atomically; parent dirs are created)."""
    target = Path(path)
    _atomic_write_text(target, waveform_to_csv(wf))
    return target


# --------------------------------------------------------------------------------------------
# Waveform store
# --------------------------------------------------------------------------------------------


class WaveformNotFound(Edu09Error, KeyError):
    """Unknown or evicted waveform id."""

    def __str__(self) -> str:
        return str(self.args[0]) if self.args else ""


class WaveformStore:
    """Bounded, thread-safe, insertion-ordered store of waveforms keyed by ``waveform_id``."""

    def __init__(self, capacity: int = DEFAULT_STORE_CAPACITY) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ValueError(f"capacity must be an int >= 1, got {capacity!r}")
        self._capacity = capacity
        self._items: OrderedDict[str, Waveform] = OrderedDict()
        self._lock = threading.Lock()

    @property
    def capacity(self) -> int:
        return self._capacity

    def add(self, wf: Waveform) -> str:
        """Store ``wf`` (re-adding an id replaces it and makes it the newest); returns its id."""
        wid = wf.waveform_id
        with self._lock:
            self._items.pop(wid, None)
            self._items[wid] = wf
            while len(self._items) > self._capacity:
                self._items.popitem(last=False)
        return wid

    def get(self, waveform_id: str = "latest") -> Waveform:
        """Fetch by id, or ``"latest"``. Raises ``WaveformNotFound``."""
        with self._lock:
            if waveform_id == "latest":
                if not self._items:
                    raise WaveformNotFound("no waveforms stored yet")
                return next(reversed(self._items.values()))
            try:
                return self._items[waveform_id]
            except KeyError:
                raise WaveformNotFound(
                    f"waveform {waveform_id!r} not found or evicted "
                    f"(store holds last {self._capacity})"
                ) from None

    def latest(self) -> Waveform | None:
        with self._lock:
            return next(reversed(self._items.values()), None)

    def snapshot(self) -> builtins.list[Waveform]:
        """Snapshot, oldest first."""
        with self._lock:
            return [*self._items.values()]

    def ids(self) -> list[str]:
        with self._lock:
            return [*self._items]

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def __contains__(self, waveform_id: object) -> bool:
        with self._lock:
            return waveform_id in self._items


__all__ = [
    "CALIBRATION_FILENAME",
    "CSV_COLUMNS",
    "DEFAULT_STORE_CAPACITY",
    "WaveformNotFound",
    "WaveformStore",
    "backup_calibration",
    "default_calibration_path",
    "default_config_dir",
    "default_data_dir",
    "default_export_dir",
    "export_csv",
    "import_vendor_ini",
    "load_calibration",
    "resolve_export_path",
    "save_calibration",
    "waveform_to_csv",
]
