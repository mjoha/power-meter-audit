"""Command line front end for the dual power source comparison."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from power_meter_audit.live.analysis import Thresholds, analyse_session, format_comparison_report
from power_meter_audit.live.harness import build_runner, run_simulated_session
from power_meter_audit.live.protocol import PRESETS, Protocol
from power_meter_audit.live.runner import (
    ControlWarning,
    LiveStatus,
    SegmentStarted,
    SessionStarted,
)
from power_meter_audit.live.session import SessionLog
from power_meter_audit.live.simulator import MeterModel, SimulatedRig
from power_meter_audit.live.sources import RealClock


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="power-meter-compare",
        description=(
            "Run a structured ERG protocol against a trainer while recording a second "
            "power meter, then report how well the two agree across power and cadence."
        ),
    )
    p.add_argument("--protocol", choices=sorted(PRESETS), default="standard")
    p.add_argument("--watts", type=int, nargs="+", metavar="W", help="Override the power ladder")
    p.add_argument("--cadences", type=int, nargs="+", default=[70, 90], metavar="RPM")
    p.add_argument("--seconds-per-cadence", type=int, help="Hold time for each cadence half")
    p.add_argument("--warmup", type=int, metavar="S", help="Warm-up seconds (0 to skip)")
    p.add_argument("--cadence-tolerance", type=float, default=5.0, metavar="RPM")

    p.add_argument("--simulate", action="store_true", help="Run against a synthetic rig")
    p.add_argument("--sim-pedal-scale", type=float, default=1.0)
    p.add_argument("--sim-pedal-torque-gain", type=float, default=0.0)
    p.add_argument("--sim-left-fraction", type=float, default=0.5)

    p.add_argument("--scan", action="store_true", help="List nearby BLE power devices and exit")
    p.add_argument("--trainer-address", metavar="ADDR", help="BLE address of the FTMS trainer")
    p.add_argument("--ant-device-id", type=int, default=0, help="ANT+ id of the pedals (0 = any)")
    p.add_argument("--pedals-ble", metavar="ADDR", help="Use BLE for the pedals instead of ANT+")

    p.add_argument("--out", type=Path, metavar="PREFIX", help="Write <PREFIX>.json and <PREFIX>.csv")
    p.add_argument("--analyse", type=Path, metavar="FILE", help="Re-analyse a saved session and exit")
    return p


def _build_protocol(args: argparse.Namespace) -> Protocol:
    kwargs: dict = {"cadences": tuple(args.cadences)}
    if args.watts:
        kwargs["watts"] = args.watts
    if args.seconds_per_cadence:
        kwargs["seconds_per_cadence"] = args.seconds_per_cadence
    if args.warmup is not None:
        kwargs["warmup_s"] = args.warmup
    protocol = PRESETS[args.protocol](**kwargs)
    protocol.cadence_tolerance_rpm = args.cadence_tolerance
    return protocol


def _make_reporter(tolerance: float):
    state = {"last_line": ""}

    def report(event) -> None:
        if isinstance(event, SessionStarted):
            print(f"Protocol '{event.protocol_name}': {event.total_duration_s / 60:.0f} min total")
        elif isinstance(event, SegmentStarted):
            seg = event.segment
            target = f" at {seg.target_rpm} rpm" if seg.target_rpm else " (free cadence)"
            print(f"\n>> {seg.label}: {seg.target_watts} W{target}, {seg.duration_s / 60:.1f} min")
        elif isinstance(event, ControlWarning):
            print(f"\n!! {event.message}", file=sys.stderr)
        elif isinstance(event, LiveStatus):
            line = _status_line(event, tolerance)
            if line != state["last_line"]:
                state["last_line"] = line
                print(f"\r{line:<100}", end="", flush=True)

    return report


def _status_line(status: LiveStatus, tolerance: float) -> str:
    def fmt(sample, unit: str) -> str:
        if sample is None or sample.watts is None:
            return "  —"
        return f"{sample.watts:4.0f}{unit}"

    cue = ""
    if status.cadence_target is not None:
        if status.cadence_error is None:
            cue = "no cadence"
        elif status.cadence_error < -tolerance:
            cue = f"SPIN UP -> {status.cadence_target}"
        elif status.cadence_error > tolerance:
            cue = f"SLOW DOWN -> {status.cadence_target}"
        else:
            cue = "cadence ok"

    rpm = ""
    if status.trainer is not None and status.trainer.cadence is not None:
        rpm = f"{status.trainer.cadence:3.0f}rpm"

    phase = "measuring" if status.measuring else "settling "
    return (
        f"{phase} {status.remaining_s:5.0f}s | trainer {fmt(status.trainer, 'W')} {rpm} | "
        f"pedals {fmt(status.pedals, 'W')} | {cue}"
    )


async def _run_live(args: argparse.Namespace, protocol: Protocol) -> SessionLog:
    from power_meter_audit.live.devices import AntPlusPedals, BlePedals, FtmsTrainer

    if not args.trainer_address:
        raise SystemExit("error: --trainer-address is required for a live run (try --scan)")

    clock = RealClock()
    trainer = FtmsTrainer(args.trainer_address, clock)
    pedals = (
        BlePedals(args.pedals_ble, clock)
        if args.pedals_ble
        else AntPlusPedals(clock, device_id=args.ant_device_id)
    )
    runner = build_runner(
        protocol, trainer, pedals, clock=clock, on_event=_make_reporter(protocol.cadence_tolerance_rpm)
    )
    return await runner.run()


async def _run_simulated(args: argparse.Namespace, protocol: Protocol) -> SessionLog:
    rig = SimulatedRig(
        pedal_model=MeterModel(
            left_fraction=args.sim_left_fraction,
            scale=args.sim_pedal_scale,
            torque_gain=args.sim_pedal_torque_gain,
            noise_w=3.0,
        )
    )
    return await run_simulated_session(protocol, rig=rig)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.scan:
        from power_meter_audit.live.devices import scan

        for address, name in asyncio.run(scan()):
            print(f"{address}  {name}")
        return 0

    if args.analyse:
        log = SessionLog.load_json(args.analyse)
        print(format_comparison_report(analyse_session(log, Thresholds())))
        return 0

    protocol = _build_protocol(args)
    if args.simulate:
        log = asyncio.run(_run_simulated(args, protocol))
    else:
        log = asyncio.run(_run_live(args, protocol))
    print()

    report = analyse_session(log, Thresholds())
    print(format_comparison_report(report))

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        log.save_json(args.out.with_suffix(".json"))
        log.save_csv(args.out.with_suffix(".csv"))
        print(f"Wrote {args.out.with_suffix('.json')} and {args.out.with_suffix('.csv')}")

    return 0 if report.consistency.value == "green" else 1


if __name__ == "__main__":
    raise SystemExit(main())
