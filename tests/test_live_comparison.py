"""Tests for the live dual-source comparison, run against a simulated rig.

The simulator injects known faults that the analysis is not told about, so the
assertions check that the analysis recovers them from the recorded samples.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from power_meter_audit.compare_cli import main as compare_main
from power_meter_audit.live.analysis import Verdict, analyse_session, format_comparison_report
from power_meter_audit.live.harness import run_simulated_session
from power_meter_audit.live.protocol import (
    CadenceSegment,
    Protocol,
    Step,
    quick_protocol,
    standard_protocol,
)
from power_meter_audit.live.session import SessionLog
from power_meter_audit.live.simulator import MeterModel, RiderModel, SimulatedRig
from power_meter_audit.live.sources import PEDALS, TRAINER


def _protocol(watts=(150, 250, 350), seconds=90, warmup=0) -> Protocol:
    return Protocol(
        name="test",
        warmup_s=warmup,
        steps=[
            Step(
                target_watts=w,
                segments=(CadenceSegment(70, seconds), CadenceSegment(90, seconds)),
                label=f"{w}W",
            )
            for w in watts
        ],
    )


def _run(protocol: Protocol, rig: SimulatedRig | None = None) -> SessionLog:
    return asyncio.run(run_simulated_session(protocol, rig=rig))


def test_timeline_splits_steps_into_cadence_cells():
    protocol = _protocol(watts=(200,), seconds=120, warmup=60)
    timeline = protocol.timeline()

    assert [seg.is_warmup for seg in timeline] == [True, False, False]
    warmup, first, second = timeline
    assert warmup.end_s == 60
    assert (first.target_rpm, second.target_rpm) == (70, 90)
    assert first.start_s == 60 and first.end_s == 180
    # A power change settles for longer than a cadence change.
    assert first.measure_from_s == 60 + protocol.power_settle_s
    assert second.measure_from_s == 180 + protocol.cadence_settle_s
    assert second.cell_label == "200W @90rpm"


def test_settle_window_never_eats_a_whole_segment():
    protocol = _protocol(watts=(200,), seconds=20, warmup=0)
    first = protocol.timeline()[0]
    assert first.measure_duration_s >= first.duration_s * 0.5


def test_matched_meters_read_as_consistent():
    log = _run(_protocol())
    report = analyse_session(log)

    assert report.consistency == Verdict.GREEN
    # Pedals double a balanced left leg; the trainer sits downstream of the chain.
    assert report.mean_ratio is not None
    assert 1.01 < report.mean_ratio < 1.05
    assert report.offset == Verdict.GREEN
    assert all(cell.usable for cell in report.cells)


def test_constant_pedal_scale_error_stays_consistent_but_flags_offset():
    rig = SimulatedRig(pedal_model=MeterModel(left_fraction=0.5, scale=1.15, noise_w=3.0))
    report = analyse_session(_run(_protocol(), rig))

    # A pure scale error shifts every cell equally, so consistency is unaffected.
    assert report.consistency == Verdict.GREEN
    assert report.offset == Verdict.RED
    assert report.mean_ratio is not None and report.mean_ratio > 1.15
    assert report.slope is not None and report.slope > 1.1


def test_torque_dependent_error_is_caught_as_inconsistency():
    rig = SimulatedRig(
        pedal_model=MeterModel(left_fraction=0.5, torque_gain=0.003, noise_w=3.0)
    )
    report = analyse_session(_run(_protocol(), rig))

    assert report.consistency == Verdict.RED
    assert report.ratio_spread is not None and report.ratio_spread > 0.06
    # The extremes of the ladder are furthest from the session mean; the middle
    # of a linear trend sits on it, so only the ends go red.
    assert any(cell.verdict == Verdict.RED for cell in report.cells)
    assert report.cells[0].verdict == Verdict.RED
    assert report.cells[-2].verdict == Verdict.RED

    # Same power, different cadence, therefore different torque: the 70 rpm half
    # is loaded harder and must read higher than the 90 rpm half.
    assert set(report.ratio_by_cadence) == {70, 90}
    assert report.ratio_by_cadence[70] > report.ratio_by_cadence[90]
    assert any("crank torque" in note for note in report.notes)


def test_ratio_rises_with_power_under_a_torque_dependent_fault():
    rig = SimulatedRig(
        pedal_model=MeterModel(left_fraction=0.5, torque_gain=0.003, noise_w=3.0)
    )
    report = analyse_session(_run(_protocol(watts=(150, 250, 350)), rig))

    at_70 = {c.target_watts: c.ratio for c in report.usable_cells if c.target_rpm == 70}
    assert at_70[150] < at_70[250] < at_70[350]


def test_cadence_guide_is_tracked_and_off_cadence_cells_are_excluded():
    on_target = analyse_session(_run(_protocol()))
    assert all(cell.time_in_zone > 0.9 for cell in on_target.cells)

    # A rider who sits 15 rpm off the guide never enters the zone.
    rig = SimulatedRig(rider=RiderModel(cadence_bias_rpm=15.0))
    stray = analyse_session(_run(_protocol(), rig))
    assert all(not cell.usable for cell in stray.cells)
    assert all("on cadence" in cell.reason for cell in stray.cells)
    assert stray.mean_ratio is None


def test_pedal_cadence_error_is_reported():
    rig = SimulatedRig(
        pedal_model=MeterModel(left_fraction=0.5, cadence_scale=1.05, noise_w=3.0)
    )
    report = analyse_session(_run(_protocol(), rig))

    assert report.cadence_bias_rpm is not None and report.cadence_bias_rpm > 2.0
    assert any("Cadence disagrees" in note for note in report.notes)


def test_repeated_condition_reports_drift():
    protocol = _protocol(watts=(200,))
    protocol.steps.append(
        Step(
            target_watts=200,
            segments=(CadenceSegment(70, 90), CadenceSegment(90, 90)),
            label="200W repeat",
        )
    )
    report = analyse_session(_run(protocol))

    labels = {label for label, _ in report.drift}
    assert labels == {"200W @70rpm", "200W @90rpm"}
    assert all(abs(delta) < 0.03 for _, delta in report.drift)


def test_dropouts_lower_the_reported_yield():
    rig = SimulatedRig(pedal_model=MeterModel(left_fraction=0.5, dropout_rate=0.5, noise_w=3.0))
    report = analyse_session(_run(_protocol(), rig))

    assert report.yields[PEDALS] < 0.6
    assert report.yields[TRAINER] > 0.9
    assert any("expected pedals samples" in note for note in report.notes)


def test_session_round_trips_through_json(tmp_path: Path):
    log = _run(_protocol(watts=(200,)))
    path = tmp_path / "session.json"
    log.save_json(path)

    restored = SessionLog.load_json(path)
    assert len(restored.samples) == len(log.samples)
    assert [s.index for s in restored.timeline] == [s.index for s in log.timeline]

    before = analyse_session(log)
    after = analyse_session(restored)
    assert before.mean_ratio == after.mean_ratio

    csv_path = tmp_path / "session.csv"
    log.save_csv(csv_path)
    body = csv_path.read_text(encoding="utf-8")
    assert "t_s,source,watts" in body
    assert TRAINER in body and PEDALS in body


def test_text_report_renders_grid_and_verdicts():
    rig = SimulatedRig(pedal_model=MeterModel(left_fraction=0.5, torque_gain=0.003, noise_w=3.0))
    text = format_comparison_report(analyse_session(_run(_protocol(), rig)))

    assert "Dual power source comparison" in text
    assert "Consistency: RED" in text
    assert "Mean ratio by cadence" in text
    assert "@70rpm" in text and "@90rpm" in text


def test_presets_are_well_formed():
    standard = standard_protocol()
    quick = quick_protocol()

    assert standard.total_duration_s > quick.total_duration_s
    assert standard.measured_segment_count == 12  # 5 steps + a repeat, two cadences each
    assert quick.measured_segment_count == 6
    for protocol in (standard, quick):
        timeline = protocol.timeline()
        assert all(a.end_s == b.start_s for a, b in zip(timeline, timeline[1:]))
        assert timeline[-1].end_s == protocol.total_duration_s


def test_cli_simulated_run_writes_outputs(tmp_path: Path, capsys):
    out = tmp_path / "run"
    code = compare_main(
        [
            "--simulate",
            "--protocol", "quick",
            "--warmup", "0",
            "--seconds-per-cadence", "60",
            "--sim-pedal-torque-gain", "0.003",
            "--out", str(out),
        ]
    )

    assert code == 1  # inconsistent by construction
    printed = capsys.readouterr().out
    assert "Dual power source comparison" in printed

    payload = json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))
    assert payload["protocol_name"] == "quick"
    assert len(payload["samples"]) > 100
    assert out.with_suffix(".csv").exists()
