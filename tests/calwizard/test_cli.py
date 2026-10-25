"""``edu09-calibrate`` CLI (spec 4.1). Never opens the real device."""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from edu09 import storage
from edu09.calwizard import cli
from edu09.calwizard.model import Plan, load_sources


def _run(argv: list[str], answers: list[str] | None = None) -> tuple[int, str]:
    out = io.StringIO()
    it: Iterator[str] = iter(answers or [])

    def ask(prompt: str) -> str:
        out.write(prompt)
        try:
            return next(it)
        except StopIteration:
            raise EOFError from None

    code = cli.main(argv, ask=ask, out=out)
    return code, out.getvalue()


def test_plan_only_text() -> None:
    code, text = _run(["--plan-only", "--fixed", "5,label=USB", "--fixed", "1.5@10%,rev,label=AA"])
    assert code == 0
    assert "S01 zero" in text
    assert "reference USB +5 V" in text
    assert "+0.40..+0.60 V" in text
    assert "Only reverse floating sources" in text


def test_plan_only_json() -> None:
    code, text = _run(["--plan-only", "--json", "--adjustable", "0..15,rev,label=bench"])
    assert code == 0
    plan = Plan.from_dict(json.loads(text))
    assert plan.sources[0].label == "bench"
    assert plan.setups[-1].kind == "verify"


def test_plan_only_options() -> None:
    code, text = _run(
        [
            "--plan-only",
            "--json",
            "--fixed",
            "1.5,label=AA",
            "--mode",
            "quick",
            "--max-volts",
            "10",
            "--ranges",
            "0.5,1",
            "--no-verify",
            "--no-zero",
            "--trimmer",
            "--trimmer-reference",
            "open",
            "--uncalibrated",
            "typical",
        ]
    )
    assert code == 0
    plan = Plan.from_dict(json.loads(text))
    o = plan.options
    assert (o.mode, o.max_abs_v, o.ranges, o.zero, o.verify, o.uncalibrated) == (
        "quick",
        10.0,
        (0.5, 1.0),
        False,
        False,
        "typical",
    )
    assert o.trimmer.enabled and o.trimmer.placement == "after_zero"
    assert o.trimmer.reference == "open"
    assert [s.kind for s in plan.setups][0] == "trimmer"


@pytest.mark.parametrize(
    "argv",
    [
        ["--yes"],
        ["--yes", "--fixed", "5"],
        ["--json"],
        ["--plan-only", "--fixed", "0..5"],
        ["--plan-only", "--adjustable", "5"],
        ["--plan-only", "--fixed", "abc"],
        ["--plan-only", "--max-volts", "40", "--fixed", "5"],
        ["--plan-only", "--ranges", "0.3", "--fixed", "5"],
        ["--plan-only", "--fixed", "5,label=x", "--fixed", "6,label=x"],
    ],
)
def test_usage_errors(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as ei:
        cli.main(argv, ask=lambda p: "", out=io.StringIO())
    assert ei.value.code == 2


def test_save_sources_profile_is_reused() -> None:
    code, _ = _run(
        ["--plan-only", "--save-sources", "--fixed", "1.5,rev,label=AA", "--mode", "quick"]
    )
    assert code == 0
    sources, opts = load_sources()
    assert [s.label for s in sources] == ["AA"] and opts.mode == "quick"
    code, text = _run(["--plan-only"])
    assert code == 0
    assert "saved source profile" in text and "reference AA" in text


def test_plan_only_without_sources_is_zero_only() -> None:
    code, text = _run(["--plan-only"])
    assert code == 0
    assert "only the zero" in text


def test_simulate_yes_end_to_end(tmp_path: Path) -> None:
    path = tmp_path / "cal.json"
    code, text = _run(
        [
            "--simulate",
            "--sim-unit",
            "unit1",
            "--sim-clock",
            "virtual",
            "--yes",
            "--adjustable",
            "0..15,rev,label=bench",
            "--mode",
            "quick",
            "--calibration",
            str(path),
            "--trimmer",
        ]
    )
    assert code == 0, text
    assert "verify passed" in text
    cal = storage.load_calibration(path)
    assert cal.source == "measured"
    assert cal.range_gain and cal.range_gain_neg
    assert cal.meta is not None and cal.meta["tool"] == "edu09-calibrate"
    assert "session log:" in text


def test_simulate_interactive_quit_saves_nothing(tmp_path: Path) -> None:
    path = tmp_path / "cal.json"
    code, text = _run(
        [
            "--simulate",
            "--sim-clock",
            "virtual",
            "--fixed",
            "1.5,rev,label=AA",
            "--calibration",
            str(path),
            "--ranges",
            "0.5",
        ],
        answers=["y", "y", "q"],
    )
    assert code == 1
    assert "nothing was saved" in text
    assert not path.exists()


def test_simulate_interactive_full_flow(tmp_path: Path) -> None:
    path = tmp_path / "cal.json"
    # start, zero, then Enter = simulated reading for every step, save, verify reading
    answers = ["y", "y", "", "", "y", ""]
    code, text = _run(
        [
            "--simulate",
            "--sim-clock",
            "virtual",
            "--fixed",
            "1.5,rev,label=AA",
            "--calibration",
            str(path),
            "--ranges",
            "0.5,1",
            "--mode",
            "quick",
        ],
        answers=answers,
    )
    assert code == 0, text
    assert "Multimeter reading in V" in text
    assert "Save this calibration?" in text
    assert path.exists()


def test_simulate_interactive_decline_save(tmp_path: Path) -> None:
    path = tmp_path / "cal.json"
    code, text = _run(
        [
            "--simulate",
            "--sim-clock",
            "virtual",
            "--fixed",
            "1.5,rev,label=AA",
            "--calibration",
            str(path),
            "--ranges",
            "0.5",
            "--mode",
            "quick",
            "--no-verify",
        ],
        answers=["y", "y", "", "", "n"],
    )
    assert code == 1
    assert "not saved" in text
    assert not path.exists()


def test_not_started(tmp_path: Path) -> None:
    code, text = _run(["--simulate", "--fixed", "1.5"], answers=["n"])
    assert code == 1 and "not started" in text


def test_help_epilog_has_single_percent(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    text = capsys.readouterr().out
    assert "VOLTS[@TOL%]" in text and "%%" not in text
