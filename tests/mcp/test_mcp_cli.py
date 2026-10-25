"""``edu09-mcp`` CLI: argument handling, DeviceBusy message, stdio end-to-end (simulator only)."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest
from mcp import Client, StdioServerParameters
from mcp_helpers import run_client

import edu09.mcp_server.__main__ as cli
from edu09.device.lock import OwnerInfo
from edu09.device.scope import Scope
from edu09.errors import DeviceBusy, DeviceNotFound


def _fail_open(exc: BaseException) -> Any:
    def fake_open(*a: Any, **kw: Any) -> Scope:
        raise exc

    return fake_open


def test_console_script_entry_point() -> None:
    text = (Path(__file__).parents[2] / "pyproject.toml").read_text(encoding="utf-8")
    assert 'edu09-mcp = "edu09.mcp_server.__main__:main"' in text


def test_parser_defaults() -> None:
    a = cli.build_parser().parse_args([])
    assert (a.http, a.host, a.port, a.simulate, a.allow_calibration, a.export_dir) == (
        False,
        "127.0.0.1",
        8765,
        None,
        False,
        None,
    )
    a = cli.build_parser().parse_args(["--simulate"])
    assert a.simulate == "sine:1khz:2vpp"


def test_bad_simulate_spec(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--simulate", "wobble:1khz"]) == cli.EXIT_BUSY
    cap = capsys.readouterr()
    assert cap.out == ""
    assert "unknown signal kind" in cap.err


def test_remote_host_refused(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--http", "--host", "0.0.0.0", "--simulate"]) == cli.EXIT_BUSY
    assert "--allow-remote" in capsys.readouterr().err


def test_device_busy_with_owner_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    owner = OwnerInfo(pid=4242, kind="gui", mcp_url="http://127.0.0.1:8765/mcp")
    exc = DeviceBusy(f"EDU09 is in use by {owner.describe()}", owner=owner)
    monkeypatch.setattr(Scope, "open", _fail_open(exc))
    assert cli.main(["--no-bridge"]) == cli.EXIT_BUSY  # never probe a real owner's URL here
    cap = capsys.readouterr()
    assert cap.out == ""
    assert "pid 4242" in cap.err
    assert "http://127.0.0.1:8765/mcp" in cap.err
    assert "bridge" in cap.err


def test_device_busy_without_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    owner = OwnerInfo(pid=7, kind="mcp")
    monkeypatch.setattr(Scope, "open", _fail_open(DeviceBusy("EDU09 is in use", owner=owner)))
    assert cli.main(["--http"]) == cli.EXIT_BUSY
    assert "Close the other program" in capsys.readouterr().err


def test_device_not_found(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(Scope, "open", _fail_open(DeviceNotFound("no EDU09 attached")))
    assert cli.main([]) == cli.EXIT_DEVICE_ERROR
    cap = capsys.readouterr()
    assert cap.out == ""
    assert "DeviceNotFound: no EDU09 attached" in cap.err


def test_hardware_open_passes_owner_and_url(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_open(*a: Any, **kw: Any) -> Scope:
        seen.update(kw)
        raise DeviceNotFound("stop here")

    monkeypatch.setattr(Scope, "open", fake_open)
    cli.main(["--http", "--port", "9999"])
    assert seen == {"owner_kind": "mcp", "mcp_url": "http://127.0.0.1:9999/mcp"}


@pytest.mark.slow
def test_stdio_end_to_end_simulated(tmp_path: Path) -> None:
    """Real subprocess over stdio: proves stdout carries only MCP messages."""
    env = dict(os.environ)
    env["EDU09_RUNTIME_DIR"] = str(tmp_path / "runtime")
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "edu09.mcp_server",
            "--simulate",
            "square:500hz:4vpp",
            "--export-dir",
            str(tmp_path / "exp"),
        ],
        env=env,
    )

    async def fn(c: Client) -> tuple[list[str], str, str]:
        names = [t.name for t in (await c.list_tools()).tools]
        st = await c.call_tool("get_status", {})
        acq = await c.call_tool("acquire", {"max_points": 50})
        return names, st.content[0].text, acq.content[0].text  # type: ignore[union-attr]

    names, st, acq = run_client(params, fn)
    assert "acquire" in names
    assert '"simulated":true' in st
    assert '"waveform_id":"w' in acq
