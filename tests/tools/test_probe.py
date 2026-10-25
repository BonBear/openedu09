"""Tests for edu09.probe (scripted fake device; no hardware)."""

from __future__ import annotations

import json

import pytest

from edu09 import probe
from edu09.errors import DeviceDisconnected
from edu09.protocol import tables
from edu09.transport import ReplayTransport, TransportInfo
from edu09.transport.replay import load_records


def pad(b: bytes) -> bytes:
    return b + bytes(64 - len(b))


class FakeScope:
    """Minimal device side: version reply, complete status after arm, 16 data reports."""

    def __init__(self, never_complete: bool = False, first_data: int = 0x64) -> None:
        self.info = TransportInfo(kind="fake")
        self.written: list[bytes] = []
        self.q: list[bytes] = []
        self.opened = False
        self.closed = False
        self.never_complete = never_complete
        self.first_data = first_data

    def open(self) -> None:
        self.opened = True

    def close(self) -> None:
        self.closed = True

    def write(self, payload: bytes) -> None:
        assert len(payload) == 64
        self.written.append(bytes(payload))
        op = payload[0]
        if op == tables.OP_VERSION:
            self.q.append(b"1.01" + bytes([0xAA] * 60))
        elif op == tables.OP_ARM and not self.never_complete:
            self.q.append(pad(bytes([0x64, 0xC8, 1])))
        elif op == tables.OP_READOUT:
            for i in range(16):
                r = bytearray(range(i, i + 64))
                if i == 0:
                    r[0] = self.first_data
                self.q.append(bytes(r))

    def read(self, timeout_s: float) -> bytes | None:
        return self.q.pop(0) if self.q else None


def run(argv, scope=None, tmp_path=None, input_fn=None):
    scope = scope or FakeScope()
    lines: list[str] = []
    args = [*argv]
    if tmp_path is not None and "--no-capture" not in args:
        args += ["--captures-dir", str(tmp_path)]
    rc = probe.main(
        args,
        transport_factory=lambda a: scope,
        out=lines.append,
        input_fn=input_fn or (lambda prompt: ""),
    )
    return rc, scope, lines


def test_parse_hex_forms():
    assert probe.parse_hex(["0E", "80,07"]) == b"\x0e\x80\x07"
    assert probe.parse_hex(["0x0e8007"]) == b"\x0e\x80\x07"
    assert probe.parse_hex(["0e:80:07"]) == b"\x0e\x80\x07"
    for bad in (["zz"], ["0"], [""], ["0e 8"]):
        with pytest.raises(ValueError):
            probe.parse_hex(bad)


def test_pad_report_limits():
    assert probe.pad_report(b"\x0f") == pad(b"\x0f")
    with pytest.raises(ValueError):
        probe.pad_report(bytes(65))
    with pytest.raises(ValueError):
        probe.pad_report(b"")


def test_version_is_allowed_by_default_and_logs_jsonl(tmp_path):
    rc, scope, lines = run(["version"], tmp_path=tmp_path)
    assert rc == 0
    assert scope.opened and scope.closed
    assert scope.written == [pad(b"\x0f")]
    assert any("'1.01'" in ln for ln in lines)
    files = list(tmp_path.glob("*-version.jsonl"))
    assert len(files) == 1
    recs = load_records(files[0])  # loadable by the replay machinery
    assert [r.dir for r in recs] == ["out", "in"]
    assert recs[0].data == pad(b"\x0f")
    assert json.loads(files[0].read_text().splitlines()[0])["note"]


def test_version_log_replays_through_replay_transport(tmp_path):
    cap = tmp_path / "v.jsonl"
    run(["version", "--capture", str(cap)])
    t = ReplayTransport(cap, strict=True)
    t.open()
    t.write(pad(b"\x0f"))
    assert (t.read(0.1) or b"")[:4] == b"1.01"


def test_no_capture_writes_nothing(tmp_path):
    rc, _, _ = run(["version", "--no-capture", "--captures-dir", str(tmp_path)])
    assert rc == 0
    assert list(tmp_path.iterdir()) == []


def test_listen_sends_nothing():
    scope = FakeScope()
    scope.q.append(pad(b"\x64\xc8\x01"))
    rc, scope, lines = run(["listen", "--seconds", "0.05", "--no-capture"], scope)
    assert rc == 0 and scope.written == []
    assert any("1 report(s)" in ln for ln in lines)


@pytest.mark.parametrize(
    "argv",
    [
        ["config", "0E 80 07 0A 04 80 80 80 08 02"],
        ["send", "0b"],
        ["send", "09"],
        ["acquire"],
    ],
)
def test_acquire_class_needs_flag(argv):
    rc, scope, lines = run([*argv, "--no-capture"])
    assert rc == 2
    assert not scope.opened and scope.written == []
    assert any("--allow-acquire" in ln for ln in lines)


@pytest.mark.parametrize("op", ["0c", "14", "55"])
def test_unknown_needs_allow_unknown(op):
    rc, scope, _ = run(["send", op, "--allow-acquire", "--no-capture"])
    assert rc == 2 and not scope.opened and scope.written == []


@pytest.mark.parametrize("answer", ["", "yes", "i understand"])
def test_unknown_needs_typed_confirmation(answer):
    rc, scope, _ = run(["send", "0c", "--allow-unknown", "--no-capture"], input_fn=lambda p: answer)
    assert rc == 2 and scope.written == []


def test_unknown_eof_on_stdin_refuses():
    def eof(prompt: str) -> str:
        raise EOFError

    rc, scope, _ = run(["send", "0c", "--allow-unknown", "--no-capture"], input_fn=eof)
    assert rc == 2 and scope.written == []


def test_unknown_with_confirmation_sends():
    rc, scope, _ = run(
        ["send", "0c", "--allow-unknown", "--no-capture"],
        input_fn=lambda p: probe.CONFIRM_PHRASE,
    )
    assert rc == 0 and scope.written == [pad(b"\x0c")]


def test_config_sends_protocol_example(tmp_path):
    rc, scope, lines = run(
        ["config", "0E", "80", "07", "0A", "04", "80", "80", "80", "08", "02", "--allow-acquire"],
        tmp_path=tmp_path,
    )
    assert rc == 0
    assert scope.written == [pad(bytes.fromhex("0e80070a0480808008" + "02"))]
    assert any("decoded config" in ln for ln in lines)


def test_send_rejects_oversized_payload():
    rc, scope, _ = run(["send", "0f" * 65, "--no-capture"])
    assert rc == 1 and scope.written == []


def test_bad_hex_is_an_error_not_a_crash():
    rc, scope, lines = run(["send", "xyz", "--no-capture"])
    assert rc == 1 and not scope.opened
    assert any(ln.startswith("error:") for ln in lines)


def test_acquire_full_sequence(tmp_path):
    rc, scope, lines = run(
        ["acquire", "--allow-acquire", "--timebase", "2ms", "--vdiv", "1", "--count", "2"],
        tmp_path=tmp_path,
    )
    assert rc == 0
    ops = [w[0] for w in scope.written]
    assert ops == [0x0E, 0x09, 0x0B, 0x0A] * 2
    # protocol.md 10.3 config for 1 V/div DC, level 0x80, 2 ms, trigger on, rising; byte 4 is
    # the neutral CH2 constant instead of the doc's 0x04
    cfg = bytearray(scope.written[0][:10])
    assert cfg[4] == tables.CH2_NEUTRAL_CFG
    cfg[4] = 0x04
    assert bytes(cfg) == bytes.fromhex("0e80070a0480808008" + "02")
    assert sum("frame ok" in ln for ln in lines) == 2
    # first data byte 0x64 must be kept as data (no mis-skip)
    assert not any("incomplete" in ln for ln in lines)


def test_acquire_options_change_config():
    rc, scope, _ = run(
        [
            "acquire", "--allow-acquire", "--no-capture", "--timebase", "500us",
            "--vdiv", "0.1", "--coupling", "ac", "--falling", "--no-trigger",
            "--trigger-level", "0x40",
        ]
    )  # fmt: skip
    cfg = scope.written[0]
    assert cfg[3] == tables.GAIN_CODES_CH1[0.1] | tables.CH1_AC_BIT
    assert cfg[7] == 0x40
    assert cfg[8] == tables.TIMEBASES[500e-6].hw_code
    assert cfg[9] == tables.FLAG_EDGE_FALLING
    assert rc == 0


def test_acquire_timeout_sends_stop_and_fails():
    rc, scope, lines = run(
        ["acquire", "--allow-acquire", "--no-capture", "--timeout", "0.1"],
        FakeScope(never_complete=True),
    )
    assert rc == 1
    assert [w[0] for w in scope.written] == [0x0E, 0x09, 0x0B, 0x08]
    assert any("no completion status" in ln for ln in lines)


def test_acquire_invalid_timebase():
    rc, scope, _ = run(["acquire", "--allow-acquire", "--no-capture", "--timebase", "3ms"])
    assert rc == 1 and scope.written == []


def test_device_error_maps_to_exit_1():
    class Dead(FakeScope):
        def write(self, payload: bytes) -> None:
            raise DeviceDisconnected("unplugged")

    rc, _, lines = run(["version", "--no-capture"], Dead())
    assert rc == 1 and any("unplugged" in ln for ln in lines)


def test_replay_subcommand_pretty_prints(tmp_path):
    cap = tmp_path / "c.jsonl"
    run(["acquire", "--allow-acquire", "--capture", str(cap)])
    rc, scope, lines = probe_replay(cap)
    assert rc == 0 and scope is None
    assert any("config [acquire]" in ln for ln in lines)
    assert any("COMPLETE mode=1" in ln for ln in lines)
    assert lines[-1] == "# 21 record(s): 4 out, 17 in"


def probe_replay(path):
    lines: list[str] = []
    rc = probe.main(["replay", str(path)], out=lines.append)
    return rc, None, lines


def test_list_with_mocked_hid(monkeypatch):
    class FakeHid:
        @staticmethod
        def enumerate(vid: int, pid: int):
            return [
                {
                    "path": b"/dev/hidraw1",
                    "vendor_id": vid,
                    "product_id": pid,
                    "serial_number": "",
                    "product_string": "EDU09",
                    "manufacturer_string": "Velleman",
                    "interface_number": 0,
                }
            ]

    lines: list[str] = []
    args = probe.build_parser().parse_args(["list"])
    assert probe.cmd_list(args, lines.append, FakeHid) == 0
    assert "/dev/hidraw1" in lines[0]

    class NoHid:
        @staticmethod
        def enumerate(vid: int, pid: int):
            return []

    assert probe.cmd_list(args, lines.append, NoHid) == 1


def test_hid_transport_gate_follows_allow_unknown(monkeypatch):
    seen: dict[str, object] = {}

    class Spy:
        def __init__(self, **kw: object) -> None:
            seen.update(kw)
            raise DeviceDisconnected("stop here")

    monkeypatch.setattr(probe, "HidTransport", Spy)
    probe.main(["version", "--no-capture"], out=lambda s: None)
    assert seen["allow_unknown_opcodes"] is False
    probe.main(["version", "--allow-unknown", "--no-capture"], out=lambda s: None)
    assert seen["allow_unknown_opcodes"] is True


# ---- payload validation (safety gate) ----------------------------------------------------------
H10_CONFIG = "0E 80 07 0A 04 80 80 80 08 02"  # protocol.md section 10


def cfg_bytes(**ch: int) -> bytearray:
    b = bytearray(pad(bytes.fromhex(H10_CONFIG)))
    idx = {"g1": 3, "g2": 4, "tb": 8, "flags": 9}
    for k, v in ch.items():
        b[idx[k]] = v
    return b


def test_protocol_section10_config_needs_only_allow_acquire():
    rc, scope, lines = run(["config", *H10_CONFIG.split(), "--allow-acquire", "--no-capture"])
    assert rc == 0, lines
    assert scope.written[0][:10] == bytes.fromhex(H10_CONFIG.replace(" ", ""))


@pytest.mark.parametrize(
    "build",
    [
        lambda: pad(bytes.fromhex("0E 80 08 0A 04 80 80 80 08 02".replace(" ", ""))),  # header
        lambda: bytes(cfg_bytes(g1=0x77)),  # bad CH1 gain
        lambda: bytes(cfg_bytes(g2=0x55)),  # bad CH2 gain
        lambda: bytes(cfg_bytes(tb=0x7F)),  # bad timebase
        lambda: bytes(cfg_bytes(flags=0x12)),  # reserved flag bits
        lambda: bytes(cfg_bytes()[:10]) + b"\x01" + bytes(53),  # non-zero tail
        lambda: bytes(cfg_bytes()[:63]) + b"\x01",  # non-zero last byte
    ],
)
def test_bad_config_needs_allow_unknown_and_confirmation(build):
    hexs = build().hex()
    rc, scope, lines = run(["send", hexs, "--allow-acquire", "--no-capture"])
    assert rc == 2 and scope.written == [] and not scope.opened
    assert "--allow-unknown" in lines[-1]
    rc, scope, lines = run(["send", hexs, "--allow-acquire", "--allow-unknown", "--no-capture"])
    assert rc == 2 and scope.written == []  # confirmation not typed
    rc, scope, lines = run(
        ["send", hexs, "--allow-unknown", "--no-capture"],
        input_fn=lambda p: probe.CONFIRM_PHRASE,
    )
    assert rc == 0 and len(scope.written) == 1


@pytest.mark.parametrize("op", ["0F", "08", "09", "0A", "0B", "0D"])
def test_known_opcode_with_trailing_bytes_is_unknown_class(op):
    rc, scope, lines = run(["send", op + " 01", "--allow-acquire", "--no-capture"])
    assert rc == 2 and scope.written == [] and "--allow-unknown" in lines[-1]
    rc, scope, _ = run(["send", op, "--allow-acquire", "--no-capture"])
    assert rc == 0 and len(scope.written) == 1


def test_config_with_bad_header_is_refused_even_with_allow_unknown():
    rc, scope, lines = run(
        ["config", "0E 81 07 0A 04 80 80 80 08 02", "--allow-unknown", "--no-capture"],
        input_fn=lambda p: probe.CONFIRM_PHRASE,
    )
    assert rc == 2 and scope.written == [] and not scope.opened
    assert "NOT sent" in lines[-1]


def test_cmd_config_decode_failure_is_refusal():
    sess = probe.Session(FakeScope(), None, allow_acquire=True, out=lambda s: None)
    args = probe.build_parser().parse_args(["config", "0E 00 00"])
    with pytest.raises(probe.UnsafeOperation):
        probe.cmd_config(sess, args)


@pytest.mark.parametrize(
    "extra",
    [["--vdiv", "3"], ["--timebase", "3ms"], ["--trigger-level", "300"]],
)
def test_acquire_bad_args_fail_before_opening(extra):
    rc, scope, lines = run(["acquire", "--allow-acquire", "--no-capture", *extra])
    assert rc == 1 and not scope.opened and scope.written == []


def test_acquire_no_trigger_ets_refused_before_opening():
    rc, scope, lines = run(
        ["acquire", "--allow-acquire", "--no-capture", "--no-trigger", "--timebase", "20us"]
    )
    assert rc == 2 and not scope.opened and "ETS" in lines[-1]


def test_acquire_configs_pass_strict_validation():
    for vdiv in tables.GAIN_CODES_CH1:
        for tb in tables.TIMEBASES:
            for coupling in ("dc", "ac"):
                for trig_on in (True, False):
                    cfg = probe.build_config(
                        f"{vdiv:g}", f"{tb:g}s", coupling, 0x80, trig_on, False
                    )
                    assert probe.payload_problem(probe.pad_report(probe.encode_config(cfg))) is None


def test_failed_write_is_logged_as_comment_and_log_stays_replayable(tmp_path):
    class Boom(FakeScope):
        def write(self, payload):
            raise DeviceDisconnected("gone")

    cap = tmp_path / "x.jsonl"
    rc, _, lines = run(["version", "--capture", str(cap)], scope=Boom())
    assert rc == 1
    text = cap.read_text()
    assert "# write failed: gone" in text
    assert load_records(cap) == []


def test_module_runs_help():
    import subprocess
    import sys

    r = subprocess.run(
        [sys.executable, "-m", "edu09.probe", "--help"], capture_output=True, text=True
    )
    assert r.returncode == 0 and "edu09-probe" in r.stdout
