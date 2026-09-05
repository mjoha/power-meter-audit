"""Signal-quality checks for power meter recordings."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from power_meter_audit.fit_loader import Activity


class QualitySeverity(str, Enum):
    INFO = "info"
    WATCH = "watch"
    SEVERE = "severe"


@dataclass
class QualityFinding:
    activity_name: str
    activity_path: str
    kind: str
    severity: QualitySeverity
    description: str
    metric: float


@dataclass
class QualityMetrics:
    dropout_rate: float
    stuck_fraction: float
    spike_rate: float
    zero_burst_fraction: float
    findings: list[QualityFinding]


def analyze_signal_quality(
    activity: Activity,
    *,
    stuck_seconds: int = 20,
    spike_watts: float = 400.0,
    zero_burst_seconds: int = 8,
) -> QualityMetrics:
    """Inspect raw record series for dropouts, stuck values, spikes, zero bursts."""
    findings: list[QualityFinding] = []
    raw_p = activity.raw_power
    raw_h = activity.raw_hr
    n = len(raw_p)
    if n == 0:
        return QualityMetrics(0.0, 0.0, 0.0, 0.0, [])

    # Dropout: HR present, power missing (while riding with HR strap).
    hr_present = 0
    power_missing_with_hr = 0
    for p, h in zip(raw_p, raw_h):
        if h is not None and h >= 40:
            hr_present += 1
            if p is None:
                power_missing_with_hr += 1
    dropout_rate = power_missing_with_hr / hr_present if hr_present else 0.0
    if dropout_rate >= 0.15:
        sev = QualitySeverity.SEVERE if dropout_rate >= 0.35 else QualitySeverity.WATCH
        findings.append(
            QualityFinding(
                activity.name,
                str(activity.path),
                "dropout",
                sev,
                f"Power missing on {dropout_rate:.0%} of samples that have HR",
                dropout_rate,
            )
        )

    # Stuck power: identical non-null watt for long stretches.
    stuck_samples = 0
    run = 1
    for i in range(1, n):
        a, b = raw_p[i - 1], raw_p[i]
        if a is not None and b is not None and a == b and a > 0:
            run += 1
            if run >= stuck_seconds:
                stuck_samples += 1
        else:
            run = 1
    stuck_fraction = stuck_samples / n
    if stuck_fraction >= 0.02 or stuck_samples >= stuck_seconds * 2:
        sev = QualitySeverity.SEVERE if stuck_fraction >= 0.08 else QualitySeverity.WATCH
        findings.append(
            QualityFinding(
                activity.name,
                str(activity.path),
                "stuck_power",
                sev,
                f"Stuck/identical power stretches (~{stuck_fraction:.1%} of samples)",
                stuck_fraction,
            )
        )

    # Spikes: large frame-to-frame jumps between valid powers.
    jumps = 0
    valid_transitions = 0
    for i in range(1, n):
        a, b = raw_p[i - 1], raw_p[i]
        if a is None or b is None:
            continue
        valid_transitions += 1
        if abs(b - a) >= spike_watts:
            jumps += 1
    spike_rate = jumps / valid_transitions if valid_transitions else 0.0
    if spike_rate >= 0.005 or jumps >= 5:
        sev = QualitySeverity.SEVERE if spike_rate >= 0.02 or jumps >= 20 else QualitySeverity.WATCH
        findings.append(
            QualityFinding(
                activity.name,
                str(activity.path),
                "spikes",
                sev,
                f"{jumps} large power jumps (≥{spike_watts:.0f} W) ({spike_rate:.2%} of transitions)",
                spike_rate,
            )
        )

    # Zero bursts after non-zero power.
    zero_burst_samples = 0
    in_burst = 0
    saw_nonzero = False
    for p in raw_p:
        if p is not None and p > 5:
            saw_nonzero = True
            in_burst = 0
            continue
        if saw_nonzero and (p is None or p <= 5):
            in_burst += 1
            if in_burst >= zero_burst_seconds:
                zero_burst_samples += 1
        else:
            in_burst = 0
    zero_burst_fraction = zero_burst_samples / n
    if zero_burst_fraction >= 0.03:
        sev = QualitySeverity.SEVERE if zero_burst_fraction >= 0.12 else QualitySeverity.WATCH
        findings.append(
            QualityFinding(
                activity.name,
                str(activity.path),
                "zero_bursts",
                sev,
                f"Zero/near-zero power bursts after pedaling (~{zero_burst_fraction:.1%} of samples)",
                zero_burst_fraction,
            )
        )

    return QualityMetrics(
        dropout_rate=dropout_rate,
        stuck_fraction=stuck_fraction,
        spike_rate=spike_rate,
        zero_burst_fraction=zero_burst_fraction,
        findings=findings,
    )

