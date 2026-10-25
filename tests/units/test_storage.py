from __future__ import annotations

import csv
import io
import json
import threading
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest

from edu09 import storage
from edu09.errors import CalibrationError
from edu09.model import Settings, Waveform
from edu09.storage import WaveformNotFound, WaveformStore
from edu09.units import Calibration, GndFit


def _wf(seq: int = 1, n: int = 16) -> Waveform:
    raw = (np.arange(n) * 3 % 256).astype(np.uint8)
    return Waveform(
        seq=seq,
        timestamp=datetime(2026, 10, 24, 12, 0, tzinfo=UTC),
        raw=raw,
        volts=raw.astype(np.float64) / 10,
        dt=1e-4,
        trigger_index=4,
        settings=Settings(),
        calibration_id="default:abc",
    )


# ---- calibration JSON ----


def test_calibration_roundtrip(tmp_path: Path) -> None:
    cal = Calibration(base_ofs=130, range_corr={0.1: 3, 1.0: -2}, gnd_fit=GndFit(2.1, -130.5))
    p = storage.save_calibration(cal, tmp_path / "sub" / "cal.json")
    assert p.is_file()
    loaded = storage.load_calibration(p)
    assert loaded == cal
    assert loaded.calibration_id == cal.calibration_id
    assert list(p.parent.iterdir()) == [p]  # no temp files left behind


def test_load_missing_gives_default(tmp_path: Path) -> None:
    assert storage.load_calibration(tmp_path / "nope.json") == Calibration()


def test_load_accepts_bare_dict(tmp_path: Path) -> None:
    p = tmp_path / "c.json"
    p.write_text(json.dumps(Calibration(base_ofs=140).to_dict()))
    assert storage.load_calibration(p).base_ofs == 140


@pytest.mark.parametrize(
    "text",
    ["{not json", '{"version": 99, "calibration": {}}', '{"base_ofs": 9999}', "[1, 2]"],
)
def test_load_invalid(tmp_path: Path, text: str) -> None:
    p = tmp_path / "c.json"
    p.write_text(text)
    with pytest.raises(CalibrationError):
        storage.load_calibration(p)


def test_save_failure_keeps_old_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "c.json"
    storage.save_calibration(Calibration(base_ofs=120), p)

    def boom(*a: object, **k: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(storage.os, "replace", boom)
    with pytest.raises(OSError):
        storage.save_calibration(Calibration(base_ofs=150), p)
    monkeypatch.undo()
    assert storage.load_calibration(p).base_ofs == 120
    assert list(tmp_path.iterdir()) == [p]


def test_backup(tmp_path: Path) -> None:
    p = tmp_path / "calibration.json"
    assert storage.backup_calibration(p) is None
    storage.save_calibration(Calibration(base_ofs=120), p)
    b = storage.backup_calibration(p)
    assert b is not None
    assert b.name.startswith("calibration.") and b.name.endswith(".bak.json")
    assert storage.load_calibration(b).base_ofs == 120


def test_default_paths_use_platformdirs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(storage.platformdirs, "user_config_dir", lambda *a, **k: str(tmp_path))
    assert storage.default_calibration_path() == tmp_path / "calibration.json"
    storage.save_calibration(Calibration(base_ofs=111))
    assert storage.load_calibration().base_ofs == 111


# ---- legacy source ----


def test_legacy_ini_import_source_loads_as_file(tmp_path: Path) -> None:
    """The vendor-INI importer was removed; a file it could have produced still loads."""
    d = Calibration(base_ofs=120).to_dict()
    d["source"] = "ini-import"
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"version": 1, "calibration": d}))
    assert storage.load_calibration(p).source == "file"
    assert Calibration.from_dict(d) == Calibration(base_ofs=120, source="file")
    with pytest.raises(CalibrationError):
        Calibration(source="ini-import")  # type: ignore[arg-type]


# ---- CSV ----


def test_csv_content(tmp_path: Path) -> None:
    wf = _wf(n=16)
    p = storage.export_csv(wf, tmp_path / "out" / "w.csv")
    lines = p.read_text().splitlines()
    comments = [ln for ln in lines if ln.startswith("#")]
    assert any('waveform_id: "w1"' in c for c in comments)
    settings_line = next(c for c in comments if c.startswith("# settings: "))
    assert json.loads(settings_line[len("# settings: ") :]) == wf.settings.to_dict()
    rows = list(csv.reader(io.StringIO("\n".join(ln for ln in lines if not ln.startswith("#")))))
    assert rows[0] == ["t_s", "volts", "raw"]
    assert len(rows) == 17
    t = wf.t()
    for i, row in enumerate(rows[1:]):
        assert float(row[0]) == t[i]
        assert float(row[1]) == wf.volts[i]
        assert int(row[2]) == wf.raw[i]


def test_resolve_export_path(tmp_path: Path) -> None:
    assert storage.resolve_export_path("a.csv", tmp_path) == tmp_path / "a.csv"
    assert storage.resolve_export_path("sub/a.csv", tmp_path) == tmp_path / "sub" / "a.csv"
    for bad in ["../a.csv", "/etc/passwd", "a/../../b", "C:\\x.csv", "..\\x", "", "a\x00b"]:
        with pytest.raises(ValueError):
            storage.resolve_export_path(bad, tmp_path)


# ---- WaveformStore ----


def test_store_basic_and_eviction() -> None:
    s = WaveformStore(capacity=3)
    with pytest.raises(WaveformNotFound, match="no waveforms"):
        s.get()
    assert s.latest() is None
    for i in range(1, 6):
        assert s.add(_wf(i)) == f"w{i}"
    assert s.ids() == ["w3", "w4", "w5"]
    assert len(s) == 3
    assert s.get().waveform_id == "w5"
    assert s.get("w3").seq == 3
    assert "w1" not in s
    with pytest.raises(WaveformNotFound, match="evicted"):
        s.get("w1")
    with pytest.raises(KeyError):
        s.get("w1")
    assert [w.seq for w in s.snapshot()] == [3, 4, 5]
    s.clear()
    assert len(s) == 0


def test_store_readd_moves_to_newest() -> None:
    s = WaveformStore(capacity=3)
    for i in (1, 2, 3):
        s.add(_wf(i))
    s.add(_wf(1))
    assert s.ids() == ["w2", "w3", "w1"]
    assert s.get().seq == 1


def test_store_default_capacity_and_validation() -> None:
    assert WaveformStore().capacity == 32
    for bad in (0, -1, True, 1.5):
        with pytest.raises(ValueError):
            WaveformStore(bad)  # type: ignore[arg-type]


def test_store_threaded() -> None:
    s = WaveformStore(capacity=8)
    errors: list[BaseException] = []

    def writer(base: int) -> None:
        try:
            for i in range(200):
                s.add(_wf(base + i))
        except BaseException as exc:
            errors.append(exc)

    def reader() -> None:
        try:
            for _ in range(500):
                s.latest()
                s.ids()
                len(s)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(b * 1000,)) for b in range(3)]
    threads += [threading.Thread(target=reader) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(s) == 8


# ---- review edge cases ----


@pytest.mark.parametrize(
    "bad",
    ["\\\\srv\\share\\x.csv", "//srv/share/x.csv", "C:x.csv", "D:/x.csv", "a\\..\\..\\b", "/x"],
)
def test_resolve_export_path_rejects_unc_and_drive(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ValueError):
        storage.resolve_export_path(bad, tmp_path)


def test_resolve_export_path_rejects_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    base = tmp_path / "exports"
    base.mkdir()
    try:
        (base / "link").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available")
    with pytest.raises(ValueError):
        storage.resolve_export_path("link/x.csv", base)
