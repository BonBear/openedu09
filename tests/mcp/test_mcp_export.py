"""export_csv tool: writes only plain *.csv names inside the export dir."""

from __future__ import annotations

from pathlib import Path

import pytest
from mcp_helpers import err, ok

from edu09.device.scope import Scope
from edu09.mcp_server.server import FastMCP, ServerOptions, build_server
from edu09.mcp_server.tools import check_export_filename

BAD_NAMES = [
    "",
    ".",
    "..",
    ".csv",
    "../escape.csv",
    "..\\escape.csv",
    "sub/x.csv",
    "sub\\x.csv",
    "/tmp/abs.csv",
    "C:\\abs.csv",
    "C:rel.csv",
    "x.csv:stream",
    "x.csv::$DATA",
    "data.txt",
    "data",
    "data.csv.",
    "data.csv ",
    " data.csv",
    "CON.csv",
    "nul.csv",
    "Com1.csv",
    "LPT9.csv",
    "aux.tar.csv",
    "a<b.csv",
    "a|b.csv",
    "a?.csv",
    "a*.csv",
    'a".csv',
    "a\x00b.csv",
    "a\nb.csv",
    "x" * 300 + ".csv",
]


@pytest.mark.parametrize("name", BAD_NAMES)
def test_check_export_filename_rejects(name: str) -> None:
    with pytest.raises(ValueError):
        check_export_filename(name)


@pytest.mark.parametrize("name", ["capture.csv", "Run 1.CSV", "a.b.csv", "console.csv", "nul_.csv"])
def test_check_export_filename_accepts(name: str) -> None:
    assert check_export_filename(name) == name


def _files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file())


def test_export_default_name(server: FastMCP, mcp_scope: Scope, export_dir: Path) -> None:
    wf = mcp_scope.acquire()
    out = ok(server, "export_csv")
    path = Path(out["path"])
    assert path.parent == export_dir
    assert path.name.startswith(f"edu09_{wf.waveform_id}_") and path.suffix == ".csv"
    assert out["rows"] == 1024
    assert out["columns"] == ["t_s", "volts", "raw"]
    text = path.read_text(encoding="utf-8")
    assert "# settings:" in text
    assert len([ln for ln in text.splitlines() if not ln.startswith("#")]) == 1025


def test_export_named_and_overwrite(server: FastMCP, mcp_scope: Scope, export_dir: Path) -> None:
    mcp_scope.acquire()
    out = ok(server, "export_csv", {"waveform_id": "w1", "filename": "run.csv"})
    assert Path(out["path"]) == export_dir / "run.csv"
    msg = err(server, "export_csv", {"filename": "run.csv"})
    assert msg.startswith("FileExistsError: ") and "overwrite" in msg
    ok(server, "export_csv", {"filename": "run.csv", "overwrite": True})
    assert _files(export_dir) == [export_dir / "run.csv"]


@pytest.mark.parametrize("name", BAD_NAMES[1:20])
def test_export_rejects_unsafe_names(
    server: FastMCP, mcp_scope: Scope, export_dir: Path, tmp_path: Path, name: str
) -> None:
    mcp_scope.acquire()
    msg = err(server, "export_csv", {"filename": name})
    assert msg.startswith("ValueError: ")
    assert _files(tmp_path) == []


def test_export_refuses_directory_and_symlink(
    server: FastMCP, mcp_scope: Scope, export_dir: Path, tmp_path: Path
) -> None:
    mcp_scope.acquire()
    (export_dir / "dir.csv").mkdir()
    assert err(server, "export_csv", {"filename": "dir.csv"}).startswith("ValueError: ")
    outside = tmp_path / "outside.csv"
    outside.write_text("keep", encoding="utf-8")
    try:
        (export_dir / "link.csv").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks not available")
    msg = err(server, "export_csv", {"filename": "link.csv", "overwrite": True})
    assert msg.startswith("ValueError: ")
    assert outside.read_text(encoding="utf-8") == "keep"


def test_export_without_waveform(server: FastMCP) -> None:
    assert err(server, "export_csv").startswith("NotFound: ")


def test_export_default_dir_is_isolated_user_data(mcp_scope: Scope) -> None:
    """Without export_dir the user data dir is used (redirected to tmp by tests/conftest.py)."""
    from edu09 import storage

    mcp_scope.acquire()
    srv = build_server(mcp_scope, ServerOptions())
    out = ok(srv, "export_csv", {"filename": "default.csv"})
    assert Path(out["path"]) == storage.default_export_dir() / "default.csv"
