"""MCP calibration wizard tools (calibration_wizard.md 4.3): gating, payload sizes, validation,
the conversational flow, single session, idle timeout and shutdown. Simulator only; the session
is a scripted fake (``cal_fake_session``) except in the end-to-end test on the real session."""

from __future__ import annotations

import json
from typing import Any

import pytest
from mcp import Client
from mcp_helpers import err, ok, run_client

from cal_fake_session import FakeSession, install
from edu09.device.scope import Scope
from edu09.mcp_server.cal_tools import (
    CAL_TOOL_NAMES,
    HUMAN_READINGS,
    IDLE_TIMEOUT_S,
    MAX_RESPONSE_BYTES,
    calibration_tools_of,
)
from edu09.mcp_server.server import FastMCP, ServerOptions, build_server, shutdown_server
from edu09.mcp_server.tools import TOOL_NAMES

AA = {"label": "AA", "volts": 1.5, "reversible": True}
USB = {"label": "USB", "volts": 5.0}
BENCH = {"label": "bench", "min_v": 0.0, "max_v": 15.0, "reversible": True}


@pytest.fixture
def cal_server(mcp_scope: Scope) -> FastMCP:
    return build_server(mcp_scope, ServerOptions(allow_calibration=True))


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> type[FakeSession]:
    return install(monkeypatch)


def _tools(srv: FastMCP) -> dict[str, Any]:
    async def fn(c: Client) -> dict[str, Any]:
        return {t.name: t for t in (await c.list_tools()).tools}

    return run_client(srv, fn)


def _size(payload: Any) -> int:
    return len(json.dumps(payload, separators=(",", ":")))


# --------------------------------------------------------------------------- gating


def test_tools_absent_without_flag(server: FastMCP) -> None:
    names = set(_tools(server))
    assert not names & set(CAL_TOOL_NAMES)
    assert calibration_tools_of(server) is None


def test_tools_present_with_flag_and_descriptions_warn(cal_server: FastMCP) -> None:
    tools = _tools(cal_server)
    assert list(tools)[len(TOOL_NAMES) :] == list(CAL_TOOL_NAMES)
    for name in ("calibration_plan", "calibration_start", "calibration_step", "calibration_submit"):
        assert HUMAN_READINGS in (tools[name].description or "")
    props = tools["calibration_submit"].input_schema["properties"]
    assert set(props) == {"action", "reading_v", "input_disconnected"}


# --------------------------------------------------------------------------- plan


def test_plan_is_pure_and_small(cal_server: FastMCP, mcp_scope: Scope) -> None:
    before = mcp_scope.calibration
    p = ok(cal_server, "calibration_plan", {"sources": [AA, USB]})
    assert [s["kind"] for s in p["setups"]][0] == "zero"
    assert any("Only reverse floating sources" in w for w in p["warnings"])
    assert {c["vdiv"] for c in p["coverage"]} == {0.1, 0.2, 0.5, 1.0, 2.0, 5.0}
    assert p["user_actions"] >= 3 and p["estimated_s"] > 0
    assert mcp_scope.calibration == before
    assert calibration_tools_of(cal_server).session is None  # type: ignore[union-attr]


def test_plan_payload_capped_for_big_plans(cal_server: FastMCP) -> None:
    p = ok(
        cal_server,
        "calibration_plan",
        {"sources": [BENCH], "mode": "full", "max_abs_v": 30, "trimmer": True},
    )
    assert len(p["setups"]) > 15
    assert _size(p) <= MAX_RESPONSE_BYTES


def test_plan_without_sources_is_zero_only(cal_server: FastMCP) -> None:
    p = ok(cal_server, "calibration_plan", {})
    assert [s["kind"] for s in p["setups"]] == ["zero"]
    assert all(c["pos"] == "missing" for c in p["coverage"])


@pytest.mark.parametrize(
    ("args", "needle"),
    [
        ({"sources": [{"label": f"s{i}", "volts": 1.0 + i} for i in range(9)]}, "at most 8"),
        ({"sources": [{"label": "x", "volts": -1.0}]}, "volts"),
        ({"sources": [{"label": "x", "min_v": 5.0, "max_v": 1.0}]}, "min_v"),
        ({"sources": [AA, AA]}, "duplicate"),
        ({"sources": [AA], "max_abs_v": 31}, "max_abs_v"),
        ({"sources": ["5V"]}, "dictionary"),
    ],
)
def test_plan_validation(cal_server: FastMCP, args: dict[str, Any], needle: str) -> None:
    msg = err(cal_server, "calibration_plan", args)
    assert needle in msg


def test_plan_mode_is_validated(cal_server: FastMCP) -> None:
    msg = err(cal_server, "calibration_plan", {"sources": [AA], "mode": "turbo"})
    assert msg.startswith("ValidationError")


# --------------------------------------------------------------------------- flow (fake)


def _drive_to_fit(srv: FastMCP) -> list[dict[str, Any]]:
    """Answer every pre-fit step with the simulated meter value; returns the step payloads."""
    seen = []
    for _ in range(40):
        step = ok(srv, "calibration_step")["step"]
        seen.append(step)
        needs = step["needs"]
        if step["state"] != "in_setup":
            return seen
        if needs == "input_disconnected":
            r = ok(srv, "calibration_submit", {"action": "measure", "input_disconnected": True})
        elif needs == "reading_v":
            r = ok(
                srv,
                "calibration_submit",
                {"action": "measure", "reading_v": step["setup"]["volts"]},
            )
        elif needs == "done":
            assert "live" in step
            r = ok(srv, "calibration_submit", {"action": "done"})
        else:
            raise AssertionError(step)
        assert _size(r) <= MAX_RESPONSE_BYTES
    raise AssertionError("did not finish")


def test_full_flow_with_fake_session(
    cal_server: FastMCP, mcp_scope: Scope, fake: type[FakeSession]
) -> None:
    started = ok(cal_server, "calibration_start", {"sources": [AA, USB], "trimmer": True})
    assert started["session_id"] == "cal1"
    assert started["step"]["needs"] == "input_disconnected"
    sess = fake.instances[0]
    assert sess.sim is not None  # sim scope: the simulated meter is attached

    # zero step refuses without the confirmation
    msg = err(cal_server, "calibration_submit", {"action": "measure"})
    assert msg.startswith("UnsafeOperation")
    assert "confirm_zero" not in " ".join(sess.calls)

    steps = _drive_to_fit(cal_server)
    assert steps[-1]["state"] == "ready_to_fit"
    assert any(s.get("live") for s in steps)  # trimmer live readout came from step calls
    assert sess.trim_n >= 1

    prev = ok(cal_server, "calibration_finish")
    assert prev["preview"]["ranges"]["1"]["gain_pos"] == 0.996
    assert "saved" not in prev
    msg = err(cal_server, "calibration_finish", {"save": True})
    assert msg.startswith("UnsafeOperation")
    assert not any(c.startswith("save") for c in sess.calls)

    saved = ok(cal_server, "calibration_finish", {"save": True, "confirm": True})
    assert saved["saved"] is False  # sim scope: applied only
    assert "save(apply_only=False)" in sess.calls
    assert any("not saved" in w for w in saved["warnings"])
    assert saved["step"]["setup"]["kind"] == "verify"

    v = saved["step"]["setup"]["volts"]
    r = ok(cal_server, "calibration_submit", {"action": "measure", "reading_v": v})
    assert r["result"]["status"] == "ok"
    assert r["step"]["state"] == "done"
    assert all(
        set(p) >= {"vdiv", "deflection_div", "err_pct", "status"} for p in r["result"]["points"]
    )


def test_reading_validation(cal_server: FastMCP, fake: type[FakeSession]) -> None:
    ok(cal_server, "calibration_start", {"sources": [USB]})
    ok(cal_server, "calibration_submit", {"action": "measure", "input_disconnected": True})
    assert "reading_v" in err(cal_server, "calibration_submit", {"action": "measure"})
    assert "30" in err(cal_server, "calibration_submit", {"action": "measure", "reading_v": 31.0})
    msg = err(cal_server, "calibration_submit", {"action": "jump"})
    assert msg.startswith("ValidationError")
    assert not any(c.startswith("submit_reading") for c in fake.instances[0].calls)


def test_retry_and_skip(cal_server: FastMCP, fake: type[FakeSession]) -> None:
    ok(cal_server, "calibration_start", {"sources": [USB]})
    sess = fake.instances[0]
    sess.fail_next = True
    r = ok(cal_server, "calibration_submit", {"action": "measure", "input_disconnected": True})
    assert r["result"]["status"] == "failed"
    assert r["step"]["phase"] == "failed"
    r = ok(cal_server, "calibration_submit", {"action": "retry"})
    assert r["step"]["phase"] == "await_input"
    r = ok(cal_server, "calibration_submit", {"action": "skip"})
    assert r["step"]["setup"]["kind"] == "reference"


def test_single_session(cal_server: FastMCP, mcp_scope: Scope, fake: type[FakeSession]) -> None:
    ok(cal_server, "calibration_start", {"sources": [USB]})
    assert "active" in err(cal_server, "calibration_start", {"sources": [USB]})
    # a second server on the same scope (e.g. the GUI wizard) is refused too
    other = build_server(mcp_scope, ServerOptions(allow_calibration=True))
    assert "active" in err(other, "calibration_start", {"sources": [USB]})
    a = ok(cal_server, "calibration_abort")
    assert a["aborted"] is True and a["log_path"]
    assert ok(cal_server, "calibration_start", {"sources": [USB]})["session_id"] == "cal2"


def test_step_without_session(cal_server: FastMCP) -> None:
    assert err(cal_server, "calibration_step").startswith("NoSession")
    assert ok(cal_server, "calibration_abort")["aborted"] is False


def test_idle_timeout_aborts(cal_server: FastMCP, fake: type[FakeSession]) -> None:
    ok(cal_server, "calibration_start", {"sources": [USB]})
    tools = calibration_tools_of(cal_server)
    assert tools is not None
    tools._last_used -= IDLE_TIMEOUT_S + 1
    msg = err(cal_server, "calibration_step")
    assert msg.startswith("NoSession") and "idle" in msg
    assert fake.instances[0].state == "aborted"
    assert ok(cal_server, "calibration_start", {"sources": [USB]})["session_id"] == "cal2"


def test_shutdown_hook_aborts(cal_server: FastMCP, fake: type[FakeSession]) -> None:
    ok(cal_server, "calibration_start", {"sources": [USB]})
    shutdown_server(cal_server)
    assert fake.instances[0].state == "aborted"
    assert "shutting down" in err(cal_server, "calibration_start", {"sources": [USB]})
    shutdown_server(cal_server)  # idempotent


def test_http_stop_aborts_session(mcp_scope: Scope, fake: type[FakeSession]) -> None:
    from edu09.mcp_server.server import start_http_in_thread

    handle = start_http_in_thread(mcp_scope, ServerOptions(allow_calibration=True), "127.0.0.1", 0)
    try:
        payload = ok(handle.url, "calibration_start", {"sources": [USB]})
        assert payload["session_id"] == "cal1"
    finally:
        handle.stop()
    assert fake.instances[0].state == "aborted"


# --------------------------------------------------------------------------- real session


def _real_session_available() -> bool:
    """True once Task B's ``CalibrationSession`` is implemented (not the published stub)."""
    from edu09.calwizard import plan_calibration
    from edu09.calwizard.session import CalibrationSession
    from edu09.transport.sim import SimTransport

    scope = Scope.open(SimTransport.virtual(seed=0), reconnect_interval_s=0.01)
    try:
        sess = CalibrationSession(scope, plan_calibration([]))
        try:
            sess.start()
        except NotImplementedError:
            return False
        sess.abort()
        return True
    finally:
        scope.close()


def test_end_to_end_real_session_on_sim(tmp_path: Any) -> None:
    if not _real_session_available():
        pytest.skip("CalibrationSession not implemented yet (Task B)")
    from edu09.transport.sim import UNIT1, SimTransport

    tr = SimTransport.virtual("dc:0v", seed=0)
    tr.device.calibration = UNIT1
    scope = Scope.open(tr, reconnect_interval_s=0.01)
    try:
        srv = build_server(scope, ServerOptions(allow_calibration=True))
        st = ok(srv, "calibration_start", {"sources": [AA, USB], "mode": "quick"})
        assert st["step"]["needs"] == "input_disconnected"
        for _ in range(30):
            step = ok(srv, "calibration_step")["step"]
            if step["state"] != "in_setup":
                break
            if step["needs"] == "input_disconnected":
                r = ok(srv, "calibration_submit", {"action": "measure", "input_disconnected": True})
            else:
                assert step["needs"] == "reading_v", step
                assert step["sim_reading_v"] is not None
                r = ok(
                    srv,
                    "calibration_submit",
                    {"action": "measure", "reading_v": step["sim_reading_v"]},
                )
            assert _size(r) <= MAX_RESPONSE_BYTES
            assert r["result"]["status"] in ("ok", "warning"), r
        assert step["state"] == "ready_to_fit"
        prev = ok(srv, "calibration_finish")
        assert prev["preview"]["ranges"]
        saved = ok(srv, "calibration_finish", {"save": True, "confirm": True})
        assert saved["calibration_id"] == scope.calibration.calibration_id
        while saved["step"]["state"] == "saved" and saved["step"]["needs"] == "reading_v":
            v = saved["step"]["sim_reading_v"]
            saved = ok(srv, "calibration_submit", {"action": "measure", "reading_v": v})
        assert saved["step"]["state"] == "done"
        shutdown_server(srv)
    finally:
        scope.close()


def test_cli_sim_unit_needs_simulate(capsys: pytest.CaptureFixture[str]) -> None:
    from edu09.mcp_server.__main__ import build_parser, main

    assert main(["--sim-unit", "unit1"]) == 2  # usage error before any device is opened
    assert "--sim-unit needs --simulate" in capsys.readouterr().err
    a = build_parser().parse_args(["--simulate", "dc:0v", "--sim-unit", "unit1"])
    assert a.sim_unit == "unit1"
