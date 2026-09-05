"""Compare activity power-vs-HR profiles to find outlier rides."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from statistics import median

import numpy as np

from power_meter_audit.fit_loader import Activity
from power_meter_audit.quality import QualityFinding, QualityMetrics, QualitySeverity, analyze_signal_quality


class Severity(str, Enum):
    OK = "ok"
    WATCH = "watch"
    SUSPECT = "suspect"


@dataclass
class HrBinProfile:
    """Median power and sample counts keyed by HR bin center (bpm)."""

    bins: dict[int, float]
    counts: dict[int, int]
    n_samples: int
    filtered_samples: int = 0


@dataclass
class BinDeviation:
    hr_bin: int
    expected_w: float
    observed_w: float
    delta_w: float
    ratio: float
    baseline_n_activities: int
    activity_n_samples: int


@dataclass
class RideAssessment:
    activity: Activity
    profile: HrBinProfile
    score: float
    mean_ratio: float
    median_ratio: float
    mean_delta_w: float
    covered_bins: int
    z_score: float
    confidence: float
    severity: Severity
    baseline_source: str
    deviations: list[BinDeviation] = field(default_factory=list)
    quality: QualityMetrics | None = None
    summary: str = ""
    examples: list[str] = field(default_factory=list)

    @property
    def quality_flag_labels(self) -> list[str]:
        if not self.quality:
            return []
        return sorted({f.kind for f in self.quality.findings})


@dataclass
class DeviceGroup:
    device_id: str
    label: str
    n_rides: int
    median_ratio: float
    mean_ratio: float
    baseline_bins: dict[int, float]
    ride_names: list[str]


@dataclass
class DeviceBinComparison:
    hr_bin: int
    devices: dict[str, float]  # device_id -> median watts


@dataclass
class AuditParams:
    hr_bin_width: int = 5
    min_bin_samples: int = 20
    min_activities_per_bin: int = 2
    min_overlap_bins: int = 3
    z_threshold: float = 1.5
    min_ratio_shift: float = 0.15
    steady_state: bool = True
    cadence_min: float = 60.0
    cadence_max: float = 110.0
    context_match_min_rides: int = 3


@dataclass
class AuditResult:
    activities: list[Activity]
    baseline_bins: dict[int, float]
    baseline_counts: dict[int, int]
    rides: list[RideAssessment]
    findings: list[RideAssessment]  # suspects (compat + CLI detail)
    watch: list[RideAssessment]
    devices: list[DeviceGroup]
    device_comparisons: list[DeviceBinComparison]
    quality_findings: list[QualityFinding]
    params: AuditParams
    notes: list[str] = field(default_factory=list)


def hr_bin(hr: float, width: int) -> int:
    low = int(hr // width) * width
    return low + width // 2


def _steady_mask(activity: Activity, steady_state: bool, cadence_min: float, cadence_max: float) -> list[bool]:
    if not steady_state or not activity.cadence:
        return [True] * len(activity.power)
    has_any_cadence = any(c is not None for c in activity.cadence)
    if not has_any_cadence:
        return [True] * len(activity.power)
    mask: list[bool] = []
    for c in activity.cadence:
        if c is None:
            mask.append(False)
            continue
        mask.append(cadence_min <= c <= cadence_max)
    # If filters wipe almost everything, fall back to unfiltered.
    if sum(mask) < max(30, int(0.2 * len(mask))):
        return [True] * len(activity.power)
    return mask


def build_profile(
    activity: Activity,
    hr_bin_width: int = 5,
    min_bin_samples: int = 20,
    *,
    steady_state: bool = True,
    cadence_min: float = 60.0,
    cadence_max: float = 110.0,
) -> HrBinProfile:
    mask = _steady_mask(activity, steady_state, cadence_min, cadence_max)
    buckets: dict[int, list[float]] = {}
    kept = 0
    for p, h, keep in zip(activity.power, activity.hr, mask):
        if not keep:
            continue
        kept += 1
        b = hr_bin(h, hr_bin_width)
        buckets.setdefault(b, []).append(p)

    bins: dict[int, float] = {}
    counts: dict[int, int] = {}
    for b, values in buckets.items():
        if len(values) < min_bin_samples:
            continue
        bins[b] = float(median(values))
        counts[b] = len(values)
    return HrBinProfile(bins=bins, counts=counts, n_samples=activity.n_samples, filtered_samples=kept)


def build_baseline(
    profiles: list[HrBinProfile],
    min_activities_per_bin: int = 2,
) -> tuple[dict[int, float], dict[int, int]]:
    by_bin: dict[int, list[float]] = {}
    for profile in profiles:
        for b, watts in profile.bins.items():
            by_bin.setdefault(b, []).append(watts)

    baseline: dict[int, float] = {}
    counts: dict[int, int] = {}
    for b, values in by_bin.items():
        if len(values) < min_activities_per_bin:
            continue
        baseline[b] = float(median(values))
        counts[b] = len(values)
    return baseline, counts


def _leave_one_out_baseline(
    profiles: list[HrBinProfile],
    skip_index: int,
    min_activities_per_bin: int,
) -> tuple[dict[int, float], dict[int, int]]:
    others = [p for i, p in enumerate(profiles) if i != skip_index]
    return build_baseline(others, min_activities_per_bin=min_activities_per_bin)


def _score_against_baseline(
    profile: HrBinProfile,
    baseline: dict[int, float],
    baseline_counts: dict[int, int],
    min_overlap_bins: int,
) -> tuple[float, float, float, float, float, list[BinDeviation]] | None:
    deviations: list[BinDeviation] = []
    for b, observed in profile.bins.items():
        expected = baseline.get(b)
        if expected is None or expected <= 0:
            continue
        deviations.append(
            BinDeviation(
                hr_bin=b,
                expected_w=expected,
                observed_w=observed,
                delta_w=observed - expected,
                ratio=observed / expected,
                baseline_n_activities=baseline_counts.get(b, 0),
                activity_n_samples=profile.counts[b],
            )
        )
    if len(deviations) < min_overlap_bins:
        return None

    weights = np.array([d.activity_n_samples for d in deviations], dtype=float)
    ratios = np.array([d.ratio for d in deviations], dtype=float)
    deltas = np.array([d.delta_w for d in deviations], dtype=float)

    mean_ratio = float(np.average(ratios, weights=weights))
    median_ratio = float(np.median(ratios))
    mean_delta = float(np.average(deltas, weights=weights))
    consistency = max(0.0, 1.0 - float(np.std(ratios)))
    score = abs(mean_ratio - 1.0) * (0.5 + 0.5 * consistency) * min(1.0, len(deviations) / 5.0)
    sample_mass = float(weights.sum())
    confidence = min(1.0, (len(deviations) / 6.0) * 0.5 + min(1.0, sample_mass / 400.0) * 0.5)
    return mean_ratio, median_ratio, mean_delta, score, confidence, sorted(deviations, key=lambda d: abs(d.delta_w), reverse=True)


def _describe(assessment: RideAssessment) -> tuple[str, list[str]]:
    direction = "higher" if assessment.mean_ratio > 1.0 else "lower"
    pct = abs(assessment.mean_ratio - 1.0) * 100.0
    summary = (
        f"{assessment.activity.name}: power reads ~{pct:.0f}% {direction} than usual "
        f"for the same HR (mean ratio {assessment.mean_ratio:.2f}, "
        f"Δ {assessment.mean_delta_w:+.0f} W across {assessment.covered_bins} HR bins, "
        f"severity={assessment.severity.value})."
    )
    examples = [
        f"  @ ~{d.hr_bin} bpm: typical {d.expected_w:.0f} W "
        f"({d.baseline_n_activities} rides) vs this ride {d.observed_w:.0f} W "
        f"({d.ratio:.2f}×, {d.delta_w:+.0f} W)"
        for d in assessment.deviations[:5]
    ]
    return summary, examples


def _assign_severity(
    assessments: list[RideAssessment],
    *,
    z_threshold: float,
    min_ratio_shift: float,
) -> None:
    scores = np.array([a.score for a in assessments], dtype=float)
    mean_s = float(scores.mean()) if len(scores) else 0.0
    std_s = float(scores.std(ddof=0)) if len(scores) else 0.0

    for a in assessments:
        shift = abs(a.mean_ratio - 1.0)
        if std_s < 1e-9:
            z = 0.0
        else:
            z = (a.score - mean_s) / std_s
        a.z_score = z

        if shift < min_ratio_shift * 0.67:
            a.severity = Severity.OK
        elif shift >= 0.25 or (shift >= min_ratio_shift and z >= z_threshold):
            a.severity = Severity.SUSPECT
        elif shift >= min_ratio_shift or z >= z_threshold * 0.75:
            a.severity = Severity.WATCH
        else:
            a.severity = Severity.OK


def _build_device_groups(rides: list[RideAssessment]) -> list[DeviceGroup]:
    by_device: dict[str, list[RideAssessment]] = {}
    for r in rides:
        by_device.setdefault(r.activity.power_device_id, []).append(r)

    groups: list[DeviceGroup] = []
    for device_id, members in sorted(by_device.items(), key=lambda kv: -len(kv[1])):
        profiles = [m.profile for m in members]
        baseline, _ = build_baseline(profiles, min_activities_per_bin=1 if len(members) == 1 else 2)
        ratios = [m.mean_ratio for m in members]
        groups.append(
            DeviceGroup(
                device_id=device_id,
                label=members[0].activity.power_device_label,
                n_rides=len(members),
                median_ratio=float(median(ratios)),
                mean_ratio=float(sum(ratios) / len(ratios)),
                baseline_bins=baseline,
                ride_names=[m.activity.name for m in members],
            )
        )
    return groups


def _device_comparisons(groups: list[DeviceGroup], min_devices: int = 2) -> list[DeviceBinComparison]:
    usable = [g for g in groups if g.n_rides >= 1 and g.device_id != "unknown"]
    if len(usable) < min_devices:
        # Still compare including unknown if multiple distinct ids.
        usable = [g for g in groups if g.n_rides >= 1]
    if len(usable) < 2:
        return []

    all_bins: set[int] = set()
    for g in usable:
        all_bins.update(g.baseline_bins.keys())

    comparisons: list[DeviceBinComparison] = []
    for b in sorted(all_bins):
        devices = {g.device_id: g.baseline_bins[b] for g in usable if b in g.baseline_bins}
        if len(devices) < 2:
            continue
        comparisons.append(DeviceBinComparison(hr_bin=b, devices=devices))
    return comparisons


def audit_activities(
    activities: list[Activity],
    *,
    hr_bin_width: int = 5,
    min_bin_samples: int = 20,
    min_activities_per_bin: int = 2,
    min_overlap_bins: int = 3,
    z_threshold: float = 1.5,
    min_ratio_shift: float = 0.15,
    steady_state: bool = True,
    cadence_min: float = 60.0,
    cadence_max: float = 110.0,
    context_match_min_rides: int = 3,
) -> AuditResult:
    params = AuditParams(
        hr_bin_width=hr_bin_width,
        min_bin_samples=min_bin_samples,
        min_activities_per_bin=min_activities_per_bin,
        min_overlap_bins=min_overlap_bins,
        z_threshold=z_threshold,
        min_ratio_shift=min_ratio_shift,
        steady_state=steady_state,
        cadence_min=cadence_min,
        cadence_max=cadence_max,
        context_match_min_rides=context_match_min_rides,
    )
    notes: list[str] = []

    quality_by_name: dict[str, QualityMetrics] = {}
    quality_findings: list[QualityFinding] = []
    for activity in activities:
        qm = analyze_signal_quality(activity)
        quality_by_name[activity.name] = qm
        quality_findings.extend(qm.findings)

    if len(activities) < 2:
        notes.append("Need at least 2 activities with power+HR to compare.")
        return AuditResult(
            activities=activities,
            baseline_bins={},
            baseline_counts={},
            rides=[],
            findings=[],
            watch=[],
            devices=[],
            device_comparisons=[],
            quality_findings=quality_findings,
            params=params,
            notes=notes,
        )

    profiles = [
        build_profile(
            a,
            hr_bin_width=hr_bin_width,
            min_bin_samples=min_bin_samples,
            steady_state=steady_state,
            cadence_min=cadence_min,
            cadence_max=cadence_max,
        )
        for a in activities
    ]
    usable = [(a, p) for a, p in zip(activities, profiles) if p.bins]
    if len(usable) < 2:
        notes.append("Fewer than 2 activities had enough samples per HR bin.")
        return AuditResult(
            activities=activities,
            baseline_bins={},
            baseline_counts={},
            rides=[],
            findings=[],
            watch=[],
            devices=[],
            device_comparisons=[],
            quality_findings=quality_findings,
            params=params,
            notes=notes,
        )

    global_baseline, global_counts = build_baseline(
        [p for _, p in usable],
        min_activities_per_bin=min_activities_per_bin,
    )

    indoor_profiles = [p for a, p in usable if a.indoor]
    outdoor_profiles = [p for a, p in usable if not a.indoor]
    indoor_baseline, indoor_counts = build_baseline(indoor_profiles, min_activities_per_bin=min_activities_per_bin)
    outdoor_baseline, outdoor_counts = build_baseline(outdoor_profiles, min_activities_per_bin=min_activities_per_bin)

    assessments: list[RideAssessment] = []
    for i, (activity, profile) in enumerate(usable):
        same_ctx_profiles = [p for a, p in usable if a.indoor == activity.indoor]
        use_context = len(same_ctx_profiles) >= context_match_min_rides
        baseline_source = "global"

        if use_context:
            # Leave-one-out within same indoor/outdoor context.
            ctx_indices = [j for j, (a, _) in enumerate(usable) if a.indoor == activity.indoor]
            local_profiles = [usable[j][1] for j in ctx_indices]
            local_i = ctx_indices.index(i)
            min_bin_acts = max(1, min_activities_per_bin - 1) if len(local_profiles) == 2 else min_activities_per_bin
            baseline, counts = _leave_one_out_baseline(local_profiles, local_i, min_bin_acts)
            baseline_source = "indoor" if activity.indoor else "outdoor"
            if len(baseline) < min_overlap_bins:
                if activity.indoor and len(indoor_baseline) >= min_overlap_bins:
                    baseline, counts = indoor_baseline, indoor_counts
                elif (not activity.indoor) and len(outdoor_baseline) >= min_overlap_bins:
                    baseline, counts = outdoor_baseline, outdoor_counts
                else:
                    baseline, counts = global_baseline, global_counts
                    baseline_source = "global_fallback"
                    notes.append(f"{activity.name}: sparse context baseline, used global")
        else:
            min_bin_acts = max(1, min_activities_per_bin - 1) if len(usable) == 2 else min_activities_per_bin
            baseline, counts = _leave_one_out_baseline([p for _, p in usable], i, min_bin_acts)
            if len(baseline) < min_overlap_bins:
                baseline, counts = global_baseline, global_counts
            baseline_source = "global"

        scored = _score_against_baseline(profile, baseline, counts, min_overlap_bins)
        if scored is None:
            continue
        mean_ratio, median_ratio, mean_delta, score, confidence, deviations = scored
        assessment = RideAssessment(
            activity=activity,
            profile=profile,
            score=score,
            mean_ratio=mean_ratio,
            median_ratio=median_ratio,
            mean_delta_w=mean_delta,
            covered_bins=len(deviations),
            z_score=0.0,
            confidence=confidence,
            severity=Severity.OK,
            baseline_source=baseline_source,
            deviations=deviations,
            quality=quality_by_name.get(activity.name),
        )
        assessments.append(assessment)

    if not assessments:
        notes.append("No activities had enough overlapping HR bins to compare.")
        return AuditResult(
            activities=activities,
            baseline_bins=global_baseline,
            baseline_counts=global_counts,
            rides=[],
            findings=[],
            watch=[],
            devices=[],
            device_comparisons=[],
            quality_findings=quality_findings,
            params=params,
            notes=notes,
        )

    _assign_severity(assessments, z_threshold=z_threshold, min_ratio_shift=min_ratio_shift)
    for a in assessments:
        a.summary, a.examples = _describe(a)

    assessments.sort(key=lambda a: abs(a.mean_ratio - 1.0), reverse=True)
    suspects = [a for a in assessments if a.severity == Severity.SUSPECT]
    watch = [a for a in assessments if a.severity == Severity.WATCH]

    devices = _build_device_groups(assessments)
    comparisons = _device_comparisons(devices)

    if not suspects and not any(f.severity == QualitySeverity.SEVERE for f in quality_findings):
        notes.append(
            "No suspect power/HR mismatches or severe quality issues "
            f"(min ratio shift {min_ratio_shift:.0%}, z≥{z_threshold})."
        )

    return AuditResult(
        activities=activities,
        baseline_bins=global_baseline,
        baseline_counts=global_counts,
        rides=assessments,
        findings=suspects,
        watch=watch,
        devices=devices,
        device_comparisons=comparisons,
        quality_findings=quality_findings,
        params=params,
        notes=notes,
    )
