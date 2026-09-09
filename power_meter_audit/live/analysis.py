"""Compare two power sources across the power/cadence grid a protocol produces.

The primary verdict is consistency, not absolute offset. A left-only pedal read
against a direct-drive trainer *should* sit a few percent high — drivetrain loss
plus whatever the rider's left/right imbalance is — and neither of those varies
with power or cadence. So a ratio that stays flat is "realistically aligned"
whatever its value, while a ratio that drifts across the grid cannot be
explained by either, and points at a real measurement fault.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from statistics import fmean

import numpy as np

from power_meter_audit.live.protocol import TimelineSegment
from power_meter_audit.live.session import SessionLog
from power_meter_audit.live.sources import PEDALS, TRAINER, Sample


class Verdict(str, Enum):
    GREEN = "green"
    AMBER = "amber"
    RED = "red"
    UNKNOWN = "unknown"


@dataclass
class Thresholds:
    # Cell verdicts measure deviation from the session mean, spread measures
    # max minus min, so the cell bands are half the spread bands.
    cell_green: float = 0.015
    cell_amber: float = 0.03
    spread_green: float = 0.03
    spread_amber: float = 0.06
    expected_ratio_low: float = 1.02
    expected_ratio_high: float = 1.09
    offset_amber_margin: float = 0.03
    min_time_in_zone: float = 0.7
    min_samples: int = 15
    cadence_agreement_rpm: float = 2.0


@dataclass
class CellResult:
    segment_index: int
    label: str
    target_watts: int
    target_rpm: int | None
    trainer_mean_w: float | None
    pedal_mean_w: float | None
    trainer_cadence_rpm: float | None
    pedal_cadence_rpm: float | None
    trainer_n: int
    pedal_n: int
    time_in_zone: float
    ratio: float | None
    ratio_stderr: float | None
    usable: bool
    verdict: Verdict = Verdict.UNKNOWN
    reason: str = ""

    @property
    def diff_pct(self) -> float | None:
        return None if self.ratio is None else (self.ratio - 1.0) * 100.0


@dataclass
class ComparisonReport:
    cells: list[CellResult]
    mean_ratio: float | None
    ratio_spread: float | None
    slope: float | None
    intercept: float | None
    r_squared: float | None
    consistency: Verdict
    offset: Verdict
    ratio_by_cadence: dict[int, float] = field(default_factory=dict)
    cadence_bias_rpm: float | None = None
    drift: list[tuple[str, float]] = field(default_factory=list)
    yields: dict[str, float] = field(default_factory=dict)
    thresholds: Thresholds = field(default_factory=Thresholds)
    notes: list[str] = field(default_factory=list)

    @property
    def usable_cells(self) -> list[CellResult]:
        return [c for c in self.cells if c.usable]


def _zone_intervals(
    trainer_samples: list[Sample],
    target_rpm: int | None,
    tolerance: float,
    window_start: float,
    window_end: float,
) -> tuple[list[tuple[float, float]], float]:
    """Time windows where the rider was on the guided cadence.

    Derived from the trainer's cadence rather than the pedals', so that a
    cadence error on the device under test cannot bias which samples are kept.
    """
    span = max(0.0, window_end - window_start)
    if target_rpm is None:
        return [(window_start, window_end)], 1.0
    if not trainer_samples or span <= 0:
        return [], 0.0

    intervals: list[tuple[float, float]] = []
    in_zone_time = 0.0
    for i, sample in enumerate(trainer_samples):
        start = sample.t
        end = trainer_samples[i + 1].t if i + 1 < len(trainer_samples) else window_end
        end = min(end, window_end)
        if end <= start:
            continue
        if sample.cadence is not None and abs(sample.cadence - target_rpm) <= tolerance:
            in_zone_time += end - start
            if intervals and abs(intervals[-1][1] - start) < 1e-9:
                intervals[-1] = (intervals[-1][0], end)
            else:
                intervals.append((start, end))

    return intervals, in_zone_time / span


def _within(intervals: list[tuple[float, float]], t: float) -> bool:
    return any(start <= t < end for start, end in intervals)


def _mean_and_sem(values: list[float]) -> tuple[float | None, float | None]:
    if not values:
        return None, None
    mean = fmean(values)
    if len(values) < 2:
        return mean, None
    sd = float(np.std(values, ddof=1))
    return mean, sd / np.sqrt(len(values))


def analyse_session(log: SessionLog, thresholds: Thresholds | None = None) -> ComparisonReport:
    thresholds = thresholds or Thresholds()
    cells: list[CellResult] = []

    for segment in log.timeline:
        if segment.is_warmup:
            continue
        cells.append(_build_cell(log, segment, thresholds))

    usable = [c for c in cells if c.usable and c.ratio is not None]
    ratios = [c.ratio for c in usable if c.ratio is not None]
    mean_ratio = fmean(ratios) if ratios else None
    spread = (max(ratios) - min(ratios)) if len(ratios) >= 2 else None

    for cell in cells:
        cell.verdict = _cell_verdict(cell, mean_ratio, thresholds)

    slope, intercept, r_squared = _regress(usable)
    consistency = _band(spread, thresholds.spread_green, thresholds.spread_amber)
    offset = _offset_verdict(mean_ratio, thresholds)

    report = ComparisonReport(
        cells=cells,
        mean_ratio=mean_ratio,
        ratio_spread=spread,
        slope=slope,
        intercept=intercept,
        r_squared=r_squared,
        consistency=consistency,
        offset=offset,
        ratio_by_cadence=_ratio_by_cadence(usable),
        cadence_bias_rpm=_cadence_bias(usable),
        drift=_drift(usable),
        yields={
            TRAINER: log.yield_rate(TRAINER, 1.0),
            PEDALS: log.yield_rate(PEDALS, 4.0),
        },
        thresholds=thresholds,
    )
    report.notes = _notes(report, thresholds)
    return report


def _build_cell(log: SessionLog, segment: TimelineSegment, thresholds: Thresholds) -> CellResult:
    start, end = segment.measure_from_s, segment.end_s
    trainer_samples = log.samples_for(TRAINER, start, end)
    pedal_samples = log.samples_for(PEDALS, start, end)

    intervals, time_in_zone = _zone_intervals(
        trainer_samples, segment.target_rpm, log.cadence_tolerance_rpm, start, end
    )

    trainer_kept = [s for s in trainer_samples if s.watts is not None and _within(intervals, s.t)]
    pedal_kept = [s for s in pedal_samples if s.watts is not None and _within(intervals, s.t)]

    trainer_mean, trainer_sem = _mean_and_sem([s.watts for s in trainer_kept if s.watts is not None])
    pedal_mean, pedal_sem = _mean_and_sem([s.watts for s in pedal_kept if s.watts is not None])
    trainer_cad, _ = _mean_and_sem([s.cadence for s in trainer_kept if s.cadence is not None])
    pedal_cad, _ = _mean_and_sem([s.cadence for s in pedal_kept if s.cadence is not None])

    ratio: float | None = None
    ratio_stderr: float | None = None
    if trainer_mean and pedal_mean and trainer_mean > 0:
        ratio = pedal_mean / trainer_mean
        if trainer_sem is not None and pedal_sem is not None:
            rel = (pedal_sem / pedal_mean) ** 2 + (trainer_sem / trainer_mean) ** 2
            ratio_stderr = ratio * float(np.sqrt(rel))

    usable = True
    reason = ""
    if time_in_zone < thresholds.min_time_in_zone:
        usable, reason = False, f"only {time_in_zone:.0%} of the window on cadence"
    elif len(trainer_kept) < thresholds.min_samples or len(pedal_kept) < thresholds.min_samples:
        usable, reason = False, f"too few samples ({len(trainer_kept)} trainer / {len(pedal_kept)} pedals)"
    elif ratio is None:
        usable, reason = False, "no usable power data"

    return CellResult(
        segment_index=segment.index,
        label=segment.cell_label,
        target_watts=segment.target_watts,
        target_rpm=segment.target_rpm,
        trainer_mean_w=trainer_mean,
        pedal_mean_w=pedal_mean,
        trainer_cadence_rpm=trainer_cad,
        pedal_cadence_rpm=pedal_cad,
        trainer_n=len(trainer_kept),
        pedal_n=len(pedal_kept),
        time_in_zone=time_in_zone,
        ratio=ratio,
        ratio_stderr=ratio_stderr,
        usable=usable,
        reason=reason,
    )


def _cell_verdict(cell: CellResult, mean_ratio: float | None, thresholds: Thresholds) -> Verdict:
    if not cell.usable or cell.ratio is None or mean_ratio is None:
        return Verdict.UNKNOWN
    return _band(abs(cell.ratio - mean_ratio), thresholds.cell_green, thresholds.cell_amber)


def _band(value: float | None, green: float, amber: float) -> Verdict:
    if value is None:
        return Verdict.UNKNOWN
    if value < green:
        return Verdict.GREEN
    if value < amber:
        return Verdict.AMBER
    return Verdict.RED


def _offset_verdict(mean_ratio: float | None, thresholds: Thresholds) -> Verdict:
    if mean_ratio is None:
        return Verdict.UNKNOWN
    if thresholds.expected_ratio_low <= mean_ratio <= thresholds.expected_ratio_high:
        return Verdict.GREEN
    margin = thresholds.offset_amber_margin
    if (
        thresholds.expected_ratio_low - margin <= mean_ratio <= thresholds.expected_ratio_high + margin
    ):
        return Verdict.AMBER
    return Verdict.RED


def _regress(cells: list[CellResult]) -> tuple[float | None, float | None, float | None]:
    points = [
        (c.trainer_mean_w, c.pedal_mean_w)
        for c in cells
        if c.trainer_mean_w is not None and c.pedal_mean_w is not None
    ]
    if len(points) < 2:
        return None, None, None
    x = np.array([p[0] for p in points], dtype=float)
    y = np.array([p[1] for p in points], dtype=float)
    slope, intercept = np.polyfit(x, y, 1)
    predicted = slope * x + intercept
    ss_res = float(np.sum((y - predicted) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r_squared = 1.0 - ss_res / ss_tot if ss_tot > 0 else None
    return float(slope), float(intercept), r_squared


def _ratio_by_cadence(cells: list[CellResult]) -> dict[int, float]:
    grouped: dict[int, list[float]] = {}
    for cell in cells:
        if cell.target_rpm is None or cell.ratio is None:
            continue
        grouped.setdefault(cell.target_rpm, []).append(cell.ratio)
    return {rpm: fmean(values) for rpm, values in sorted(grouped.items())}


def _cadence_bias(cells: list[CellResult]) -> float | None:
    diffs = [
        cell.pedal_cadence_rpm - cell.trainer_cadence_rpm
        for cell in cells
        if cell.pedal_cadence_rpm is not None and cell.trainer_cadence_rpm is not None
    ]
    return fmean(diffs) if diffs else None


def _drift(cells: list[CellResult]) -> list[tuple[str, float]]:
    """Ratio change between repeats of an identical power/cadence condition."""
    by_condition: dict[tuple[int, int | None], list[CellResult]] = {}
    for cell in cells:
        by_condition.setdefault((cell.target_watts, cell.target_rpm), []).append(cell)

    drift: list[tuple[str, float]] = []
    for (watts, rpm), group in sorted(by_condition.items()):
        if len(group) < 2:
            continue
        group.sort(key=lambda c: c.segment_index)
        first, last = group[0], group[-1]
        if first.ratio is None or last.ratio is None:
            continue
        rpm_label = f"@{rpm}rpm" if rpm is not None else ""
        drift.append((f"{watts}W {rpm_label}".strip(), last.ratio - first.ratio))
    return drift


def _notes(report: ComparisonReport, thresholds: Thresholds) -> list[str]:
    notes: list[str] = []
    skipped = [c for c in report.cells if not c.usable]
    for cell in skipped:
        notes.append(f"{cell.label}: excluded — {cell.reason}")

    if report.consistency == Verdict.RED and report.ratio_spread is not None:
        notes.append(
            f"Ratio moves {report.ratio_spread * 100:.1f} points across the grid. "
            "Imbalance and drivetrain loss are both constant, so neither explains this."
        )
    if len(report.ratio_by_cadence) >= 2:
        rpms = sorted(report.ratio_by_cadence)
        gap = report.ratio_by_cadence[rpms[-1]] - report.ratio_by_cadence[rpms[0]]
        if abs(gap) >= thresholds.cell_green:
            notes.append(
                f"Ratio differs by {gap * 100:+.1f} points between {rpms[0]} and {rpms[-1]} rpm "
                "at matched power, so the error tracks crank torque rather than power."
            )
    if report.cadence_bias_rpm is not None and abs(report.cadence_bias_rpm) >= thresholds.cadence_agreement_rpm:
        notes.append(
            f"Cadence disagrees by {report.cadence_bias_rpm:+.1f} rpm. Pedal power is derived from "
            "angular velocity, so a cadence error feeds straight into power."
        )
    for label, delta in report.drift:
        if abs(delta) >= thresholds.cell_green:
            notes.append(
                f"{label} drifted {delta * 100:+.1f} points between repeats — session drift, not calibration."
            )
    for source, rate in report.yields.items():
        if rate < 0.8:
            notes.append(f"Only {rate:.0%} of expected {source} samples arrived; check the link.")
    return notes


def comparison_to_dict(report: ComparisonReport) -> dict:
    return {
        "mean_ratio": report.mean_ratio,
        "ratio_spread": report.ratio_spread,
        "slope": report.slope,
        "intercept": report.intercept,
        "r_squared": report.r_squared,
        "consistency": report.consistency.value,
        "offset": report.offset.value,
        "ratio_by_cadence": {str(k): v for k, v in report.ratio_by_cadence.items()},
        "cadence_bias_rpm": report.cadence_bias_rpm,
        "drift": [{"condition": label, "delta": delta} for label, delta in report.drift],
        "yields": report.yields,
        "notes": report.notes,
        "cells": [
            {
                "segment_index": cell.segment_index,
                "label": cell.label,
                "target_watts": cell.target_watts,
                "target_rpm": cell.target_rpm,
                "trainer_mean_w": cell.trainer_mean_w,
                "pedal_mean_w": cell.pedal_mean_w,
                "trainer_cadence_rpm": cell.trainer_cadence_rpm,
                "pedal_cadence_rpm": cell.pedal_cadence_rpm,
                "trainer_n": cell.trainer_n,
                "pedal_n": cell.pedal_n,
                "time_in_zone": cell.time_in_zone,
                "ratio": cell.ratio,
                "ratio_stderr": cell.ratio_stderr,
                "diff_pct": cell.diff_pct,
                "usable": cell.usable,
                "verdict": cell.verdict.value,
                "reason": cell.reason,
            }
            for cell in report.cells
        ],
    }


def format_comparison_report(report: ComparisonReport) -> str:
    lines: list[str] = []
    lines.append("Dual power source comparison")
    lines.append("=" * 72)

    if report.mean_ratio is None:
        lines.append("No usable cells — nothing to compare.")
        return "\n".join(lines) + "\n"

    lines.append(
        f"Consistency: {report.consistency.value.upper()}  |  "
        f"offset: {report.offset.value.upper()}  |  "
        f"mean ratio {report.mean_ratio:.3f} ({(report.mean_ratio - 1) * 100:+.1f}%)"
    )
    if report.ratio_spread is not None:
        lines.append(f"Ratio spread across grid: {report.ratio_spread * 100:.1f} percentage points")
    if report.slope is not None:
        lines.append(
            f"Fit: pedals = {report.slope:.3f} x trainer {report.intercept:+.1f} W"
            + (f"  (R^2 {report.r_squared:.4f})" if report.r_squared is not None else "")
        )
    lines.append("")

    header = (
        f"{'cell':<22} {'trainer':>8} {'pedals':>8} {'ratio':>7} {'diff':>7} "
        f"{'zone':>6} {'n':>9}  flag"
    )
    lines.append(header)
    lines.append("-" * len(header))
    for cell in report.cells:
        if cell.ratio is None:
            lines.append(f"{cell.label:<22} {'—':>8} {'—':>8} {'—':>7} {'—':>7} "
                         f"{cell.time_in_zone:>5.0%} {cell.trainer_n:>4}/{cell.pedal_n:<4}  {cell.verdict.value}")
            continue
        lines.append(
            f"{cell.label:<22} {cell.trainer_mean_w:>8.1f} {cell.pedal_mean_w:>8.1f} "
            f"{cell.ratio:>7.3f} {cell.diff_pct:>+6.1f}% {cell.time_in_zone:>5.0%} "
            f"{cell.trainer_n:>4}/{cell.pedal_n:<4}  {cell.verdict.value}"
        )
    lines.append("")

    if report.ratio_by_cadence:
        parts = [f"{rpm} rpm: {ratio:.3f}" for rpm, ratio in report.ratio_by_cadence.items()]
        lines.append("Mean ratio by cadence — " + ",  ".join(parts))
    if report.cadence_bias_rpm is not None:
        lines.append(f"Cadence bias (pedals - trainer): {report.cadence_bias_rpm:+.2f} rpm")
    if report.drift:
        for label, delta in report.drift:
            lines.append(f"Drift at {label}: {delta * 100:+.1f} points between repeats")
    lines.append("")

    if report.notes:
        lines.append("Notes")
        lines.append("-" * 72)
        for note in report.notes:
            lines.append(f"- {note}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
