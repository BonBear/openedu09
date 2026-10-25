"""``edu09-calibrate``: interactive calibration wizard on the command line (spec 4.1).

Describe the reference voltages you can produce (``--fixed`` / ``--adjustable``, or the saved
profile, or interactively); the wizard prints the plan, tells you what to connect and asks for
the multimeter reading of every step, then fits, saves (with a backup) and verifies.

``--plan-only`` never opens a device. ``--simulate`` runs on a simulated scope whose front end
has the physical calibration ``--sim-unit`` and a simulated bench supply + multimeter;
with ``--yes`` it is fully non-interactive (CI smoke path). Real hardware never accepts ``--yes``.

Exit codes: 0 done, 1 error or aborted, 2 usage.
"""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
from collections.abc import Callable, Sequence
from typing import IO, Any, Final

from edu09.calwizard.model import (
    AdjustableSource,
    FixedSource,
    Plan,
    PlanOptions,
    SessionState,
    SetupResult,
    SetupView,
    Source,
    TrimmerConfig,
    load_sources,
    options_to_dict,
    save_sources,
)
from edu09.calwizard.planner import parse_source, plan_calibration
from edu09.errors import Edu09Error, JobAborted
from edu09.model import VoltsPerDiv

__all__ = ["main"]

PROG: Final = "edu09-calibrate"
SIM_DEFAULT_SOURCE: Final = "0..15,rev,label=simulated bench supply"
TRIMMER_HZ: Final = 4.0
YES_TRIMMER_SAMPLES: Final = 3
SIM_ADC_NOISE_COUNTS: Final = 0.7
"""ADC noise of the ``--simulate`` device (counts rms, as measured on hardware): dithers DC levels
so the simulated gains are not quantised to whole counts."""


class _Quit(Exception):
    """The user typed q."""


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "Calibrate the EDU09 with a multimeter and the reference voltages you have. "
            "Close the GUI / MCP server first (one process owns the device)."
        ),
        epilog=(
            "SPEC: --fixed VOLTS[@TOL%][,rev][,label=TEXT] (e.g. 5,label=USB  1.5@10%,rev,"
            "label=AA); --adjustable MIN..MAX[,rev][,label=TEXT] (e.g. 0..15,rev,label=bench). "
            "'rev' = the leads may be swapped (floating sources only: batteries, isolated "
            "supplies; never a source sharing ground with this PC). Max input 30 V."
        ),
    )
    p.add_argument("--fixed", action="append", default=[], metavar="SPEC", help="fixed source")
    p.add_argument(
        "--adjustable", action="append", default=[], metavar="SPEC", help="adjustable source"
    )
    p.add_argument("--mode", choices=("quick", "standard", "full"), default=None)
    p.add_argument("--max-volts", type=float, default=None, help="reference cap (default 15)")
    p.add_argument("--ranges", default=None, help="V/div ranges, e.g. 0.1,0.2,0.5")
    p.add_argument("--no-zero", action="store_true", help="skip the zero (offset) step")
    p.add_argument("--no-verify", action="store_true", help="no verify step")
    p.add_argument("--uncalibrated", choices=("keep", "typical", "unity"), default=None)
    p.add_argument(
        "--trimmer",
        nargs="?",
        const="after_zero",
        choices=("first", "after_zero"),
        default=None,
        help="add the RV1 trimmer step (default placement: after_zero)",
    )
    p.add_argument("--trimmer-vdiv", default=None, help="trimmer range (default 0.5 V/div)")
    p.add_argument("--trimmer-reference", choices=("source", "open"), default=None)
    p.add_argument("--trimmer-source", default=None, metavar="LABEL")
    p.add_argument("--plan-only", action="store_true", help="print the plan; no device")
    p.add_argument("--json", action="store_true", help="with --plan-only: the plan as JSON")
    p.add_argument("--calibration", default=None, metavar="PATH", help="calibration file")
    p.add_argument("--save-sources", action="store_true", help="save the source profile")
    p.add_argument("--simulate", action="store_true", help="simulated scope and references")
    p.add_argument("--sim-unit", choices=("ideal", "unit1"), default="unit1")
    p.add_argument(
        "--sim-clock",
        choices=("real", "virtual"),
        default="real",
        help="simulated time (virtual: instant, for tests)",
    )
    p.add_argument("--yes", action="store_true", help="with --simulate: answer everything")
    return p


# --------------------------------------------------------------------------------------------
# Plan input
# --------------------------------------------------------------------------------------------


def _sources_from_args(args: argparse.Namespace) -> list[Source]:
    out: list[Source] = []
    for spec in args.fixed:
        s = parse_source(spec)
        if not isinstance(s, FixedSource):
            raise ValueError(f"--fixed {spec!r} is an adjustable range; use --adjustable")
        out.append(s)
    for spec in args.adjustable:
        s = parse_source(spec)
        if not isinstance(s, AdjustableSource):
            raise ValueError(f"--adjustable {spec!r} needs MIN..MAX")
        out.append(s)
    return out


def _options(args: argparse.Namespace, base: PlanOptions) -> PlanOptions:
    d = options_to_dict(base)
    if args.mode is not None:
        d["mode"] = args.mode
    if args.max_volts is not None:
        d["max_abs_v"] = args.max_volts
    if args.ranges is not None:
        d["ranges"] = [float(VoltsPerDiv.parse(x.strip())) for x in args.ranges.split(",") if x]
    if args.no_zero:
        d["zero"] = False
    if args.no_verify:
        d["verify"] = False
    if args.uncalibrated is not None:
        d["uncalibrated"] = args.uncalibrated
    t = dict(d["trimmer"])
    if args.trimmer is not None:
        t["enabled"] = True
        t["placement"] = args.trimmer
    if args.trimmer_vdiv is not None:
        t["vdiv"] = float(VoltsPerDiv.parse(args.trimmer_vdiv))
    if args.trimmer_reference is not None:
        t["reference"] = args.trimmer_reference
    if args.trimmer_source is not None:
        t["source_label"] = args.trimmer_source
    return PlanOptions(
        mode=d["mode"],
        max_abs_v=d["max_abs_v"],
        ranges=tuple(d["ranges"]),
        zero=d["zero"],
        verify=d["verify"],
        trimmer=TrimmerConfig(**t),
        uncalibrated=d["uncalibrated"],
    )


def _ask_sources(ask: Callable[[str], str], out: IO[str]) -> list[Source]:
    print(
        "Describe the reference voltages you can produce (one per line, empty line to finish).\n"
        "  fixed:      VOLTS[@TOL%][,rev][,label=TEXT]   e.g. 1.5@10%,rev,label=AA battery\n"
        "  adjustable: MIN..MAX[,rev][,label=TEXT]       e.g. 0..15,rev,label=bench supply\n"
        "'rev' = you can swap the leads (floating sources only).",
        file=out,
    )
    sources: list[Source] = []
    while True:
        line = ask(f"source {len(sources) + 1}: ").strip()
        if not line:
            return sources
        if line.lower() == "q":
            raise _Quit
        try:
            sources.append(parse_source(line))
        except ValueError as exc:
            print(f"  {exc}", file=out)


def format_plan(plan: Plan) -> str:
    """Human-readable plan: setups, coverage, effort, warnings."""
    lines = [f"Calibration plan ({plan.options.mode} mode, max {plan.options.max_abs_v:g} V):"]
    for s in plan.setups:
        what: str = s.kind
        if s.volts is not None:
            what += f" {s.source_label} {s.volts:+.4g} V"
        pts = ", ".join(f"{p.vdiv:g} V/div@{p.position_div:+g}" for p in s.points[:6])
        more = f" (+{len(s.points) - 6})" if len(s.points) > 6 else ""
        lines.append(f"  {s.id} {what}: {len(s.points)} point(s) {pts}{more}")
    lines.append("Coverage (V/div: positive / negative / nonlinearity):")
    for c in plan.coverage:
        lines.append(f"  {c.vdiv:>4g}: {c.pos} / {c.neg} / {c.nonlin}")
        lines.extend(f"        - {n}" for n in c.needs)
    lines.append(
        f"{plan.user_actions} reading(s), {plan.reconnections} reconnection(s), "
        f"about {plan.estimated_s / 60:.0f} min"
    )
    lines.extend(f"WARNING: {w}" for w in plan.warnings)
    return "\n".join(lines)


# --------------------------------------------------------------------------------------------
# Session driving
# --------------------------------------------------------------------------------------------


def _print_result(r: SetupResult, out: IO[str]) -> None:
    print(f"  -> {r.status}", file=out)
    for p in r.points:
        if p.purpose == "ground":
            continue
        err = f" {p.err_pct:+.2f} %" if p.err_pct is not None else ""
        msg = f" {p.message}" if p.message else ""
        hint = f" ({p.hint})" if p.hint and p.status != "ok" else ""
        print(
            f"     {p.vdiv:g} V/div pos {p.position_div:+g}: {p.code.value}{err}{msg}{hint}",
            file=out,
        )
    for m in r.messages:
        print(f"     {m}", file=out)


def _format_fit(fit: Any) -> str:
    lines = [
        "Fit preview (g = V_true / V_read):",
        "  V/div    g+      g-   NL  before%  after%  status",
    ]
    for v, rf in sorted(fit.ranges.items()):

        def f(x: float | None, fmt: str = "{:.4f}") -> str:
            return fmt.format(x) if x is not None else "   -  "

        lines.append(
            f"  {v:>5g} {f(rf.gain_pos)} {f(rf.gain_neg)} {len(rf.neg_knots):>3} "
            f"{f(rf.before_err_pct_max, '{:7.2f}')} {f(rf.after_err_pct_max, '{:7.2f}')}  "
            f"{rf.status}"
        )
        lines.extend(f"        ! {w}" for w in rf.warnings)
    lines.extend(f"WARNING: {w}" for w in fit.warnings)
    return "\n".join(lines)


class _LineReader:
    """Reads stdin lines on a daemon thread (portable non-blocking Enter detection)."""

    def __init__(self, ask: Callable[[str], str]) -> None:
        self.q: queue.Queue[str] = queue.Queue()
        self._t = threading.Thread(target=self._run, args=(ask,), daemon=True)
        self._t.start()

    def _run(self, ask: Callable[[str], str]) -> None:
        try:
            self.q.put(ask(""))
        except (EOFError, OSError):
            self.q.put("")


def _trimmer_live(session: Any, ask: Callable[[str], str], out: IO[str], yes: bool) -> None:
    print("  live readout; press Enter when done adjusting", file=out)
    reader = None if yes else _LineReader(ask)
    n = 0
    while True:
        t0 = time.monotonic()
        tr = session.trimmer_sample()
        n += 1
        out.write(
            f"\r  scope {tr.v_meas:+.4f} V  ref {tr.v_ref:+.4f} V  dev {tr.dev_v * 1e3:+7.1f} mV "
            f"{tr.dev_pct:+6.2f} %  best {tr.best_abs_dev_pct:.2f} %  {tr.trend:<6}"
            + ("  (model ground)" if getattr(tr, "ground_source", "map") == "model" else "")
        )
        out.flush()
        if yes and n >= YES_TRIMMER_SAMPLES:
            break
        if reader is not None and not reader.q.empty():
            break
        if not yes:
            time.sleep(max(0.0, 1.0 / TRIMMER_HZ - (time.monotonic() - t0)))
    out.write("\n")
    session.trimmer_done()


def _run_session(
    session: Any, ask: Callable[[str], str], out: IO[str], yes: bool, simulated: bool
) -> int:
    from edu09.calwizard.session import CalibrationSession

    assert isinstance(session, CalibrationSession)
    session.start()
    shown: str | None = None
    while True:
        v: SetupView = session.view()
        st = v.state
        if st is SessionState.DONE:
            break
        if st.terminal:
            print(f"calibration {st.value}; nothing was saved", file=out)
            return 1
        if st is SessionState.READY_TO_FIT:
            fit = session.fit()
            print(_format_fit(fit), file=out)
            ans = "y" if yes else ask("Save this calibration? [y/N] ").strip().lower()
            if ans not in ("y", "yes"):
                session.abort()
                print("not saved; the previous calibration stays in use", file=out)
                return 1
            res = session.save(confirm=True)
            if res.saved:
                bak = f" (backup {res.backup_path})" if res.backup_path else ""
                print(f"saved to {res.path}{bak}; id {res.calibration_id}", file=out)
            else:
                print(f"applied (not written to a file); id {res.calibration_id}", file=out)
            continue
        s = v.setup
        assert s is not None
        if shown != s.id:
            shown = s.id
            print(f"\n[{v.index + 1}/{v.total}] {s.id} {s.kind}", file=out)
            print(f"  {s.instruction}", file=out)
        if v.needs == "done":
            _trimmer_live(session, ask, out, yes)
            continue
        if v.needs == "input_disconnected":
            ans = (
                "y"
                if yes
                else ask("Disconnect the probe (or clip tip to ground). Ready? [y/N/s=skip/q] ")
                .strip()
                .lower()
            )
            if ans == "q":
                raise _Quit
            if ans == "s":
                session.skip()
                continue
            if ans not in ("y", "yes"):
                continue
            print("  measuring offsets...", file=out)
            _print_result(session.confirm_zero(input_disconnected=True), out)
            continue
        if v.needs == "reading_v":
            sim = v.sim_reading_v
            if yes:
                assert sim is not None
                text = f"{sim}"
            else:
                pre = f" [Enter = simulated {sim:+.3f}]" if sim is not None else ""
                text = ask(f"Multimeter reading in V{pre} (r=retry, s=skip, q=quit): ").strip()
                if not text and sim is not None:
                    text = f"{sim}"
            low = text.lower()
            if low == "q":
                raise _Quit
            if low == "s":
                session.skip()
                continue
            if low == "r":
                session.retry()
                shown = None
                continue
            try:
                volts = float(text.replace(",", "."))
            except ValueError:
                print("  type a number in volts (e.g. 1.503), r, s or q", file=out)
                continue
            try:
                r = session.submit_reading(volts)
            except ValueError as exc:
                print(f"  {exc}", file=out)
                continue
            _print_result(r, out)
            suspect = r.extra.get("reading_suspect")
            if suspect is not None and not yes:
                ans = (
                    ask(
                        f"Reading differs by {suspect:.1f} % from the scope - did you type it "
                        "correctly? [y = accept / r = retype] "
                    )
                    .strip()
                    .lower()
                )
                if ans == "r":
                    session.retry()
                    shown = None
                    continue
            if yes and r.status == "failed" and s.kind != "verify":
                session.skip()  # a simulated run never loops on a failing step
            continue
        print(f"  unexpected step state {v.needs!r}; skipping", file=out)
        session.skip()
    summ = session.summary()
    verify = summ.get("verify")
    if verify == "fail":
        print("VERIFY FAILED", file=out)
        ans = "n" if yes else ask("Revert to the previous calibration? [y/N] ").strip().lower()
        if ans in ("y", "yes"):
            res = session.revert()
            print(f"reverted (id {res.calibration_id})", file=out)
    elif verify == "pass":
        print("verify passed", file=out)
    print("done", file=out)
    return 0


def _open_scope(args: argparse.Namespace) -> tuple[Any, Any]:
    from edu09.device.scope import Scope

    if not args.simulate:
        return Scope.open(calibration=args.calibration, owner_kind="other"), None
    from edu09.calwizard.simref import SimReference
    from edu09.transport.sim import PHYSICAL_UNITS, SimTransport, SimulatedDevice, VirtualClock

    if args.sim_clock == "virtual":
        clock = VirtualClock()
        dev = SimulatedDevice(
            "dc:0v",
            clock=clock,
            calibration=PHYSICAL_UNITS[args.sim_unit],
            seed=None,
            adc_noise_counts=SIM_ADC_NOISE_COUNTS,
        )
        tr = SimTransport(device=dev, sleep=clock.sleep)
    else:
        dev = SimulatedDevice(
            "dc:0v",
            calibration=PHYSICAL_UNITS[args.sim_unit],
            seed=None,
            adc_noise_counts=SIM_ADC_NOISE_COUNTS,
        )
        tr = SimTransport(device=dev)
    scope = Scope.open(tr, calibration=args.calibration, reconnect_interval_s=0.05)
    return scope, SimReference.for_scope(scope)


def _saved_note(session: Any, was_saved: bool) -> str:
    if was_saved:
        return "the calibration saved before stays in use"
    ps = None if session is None else session.partial_save
    if ps is not None:
        bak = f"; backup of the previous file: {ps.backup_path}" if ps.backup_path else ""
        return (
            f"the calibration file WAS written ({ps.path}, id {ps.calibration_id}){bak}. "
            "Check it, or restore the backup"
        )
    return "nothing was saved"


def main(
    argv: Sequence[str] | None = None,
    *,
    ask: Callable[[str], str] | None = None,
    out: IO[str] | None = None,
) -> int:
    """CLI entry point (``ask``/``out`` are injectable for tests)."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    o: IO[str] = out if out is not None else sys.stdout
    asker: Callable[[str], str] = ask if ask is not None else input
    if args.yes and not args.simulate:
        parser.error("--yes needs --simulate (on hardware the readings must come from you)")
    if args.json and not args.plan_only:
        parser.error("--json needs --plan-only")
    try:
        sources = _sources_from_args(args)
        base_opts = PlanOptions()
        if not sources:
            profile, saved_opts = load_sources()
            if profile:
                sources, base_opts = list(profile), saved_opts
                print(f"using the saved source profile ({len(sources)} source(s))", file=o)
        if not sources and args.simulate:
            sources = [parse_source(SIM_DEFAULT_SOURCE)]
        options = _options(args, base_opts)
    except ValueError as exc:
        parser.error(str(exc))
    try:
        if not sources and not args.plan_only:
            sources = _ask_sources(asker, o)
        plan = plan_calibration(sources, options)
    except ValueError as exc:
        parser.error(str(exc))
    except (_Quit, EOFError, KeyboardInterrupt):
        print("aborted", file=o)
        return 1
    if args.save_sources:
        p = save_sources(sources, options)
        print(f"source profile saved to {p}", file=o)
    if args.plan_only:
        if args.json:
            print(json.dumps(plan.to_dict(), indent=2), file=o)
        else:
            print(format_plan(plan), file=o)
        return 0
    print(format_plan(plan), file=o)
    try:
        if not args.yes and asker("Start? [y/N] ").strip().lower() not in ("y", "yes"):
            print("not started", file=o)
            return 1
    except (EOFError, KeyboardInterrupt):
        return 1

    from edu09.calwizard.session import CalibrationSession

    try:
        scope, sim = _open_scope(args)
    except (Edu09Error, OSError, TimeoutError) as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return 1
    session = None
    try:
        session = CalibrationSession(scope, plan, path=args.calibration, sim=sim)
        return _run_session(session, asker, o, args.yes, args.simulate)
    except (_Quit, KeyboardInterrupt, EOFError, JobAborted):
        was_saved = False
        if session is not None:
            session.abort()
            was_saved = session.summary()["saved"] is not None
        print("\naborted; " + _saved_note(session, was_saved), file=o)
        return 1
    except (Edu09Error, OSError) as exc:
        if session is not None:
            session.abort()
        print(f"{PROG}: {exc}", file=sys.stderr)
        note = _saved_note(session, session is not None and session.summary()["saved"] is not None)
        if session is not None and session.partial_save is not None:
            print(note, file=sys.stderr)
        return 1
    finally:
        scope.close()
        if session is not None and session.log_path is not None:
            print(f"session log: {session.log_path}", file=o)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
