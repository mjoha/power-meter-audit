"""Human-readable and JSON report formatting."""

from __future__ import annotations

from power_meter_audit.analyze import AuditResult, RideAssessment
from power_meter_audit.quality import QualitySeverity


def _fmt_date(assessment: RideAssessment) -> str:
    t = assessment.activity.start_time
    return t.strftime("%Y-%m-%d") if t else "—"


def _pad(text: str, width: int) -> str:
    if len(text) > width:
        return text[: width - 1] + "…"
    return text.ljust(width)


def format_text_report(result: AuditResult, top: int = 20, verbose: bool = False) -> str:
    lines: list[str] = []
    rides = result.rides
    n_suspect = sum(1 for r in rides if r.severity.value == "suspect")
    n_watch = sum(1 for r in rides if r.severity.value == "watch")
    n_quality = len(result.quality_findings)
    n_severe_q = sum(1 for f in result.quality_findings if f.severity == QualitySeverity.SEVERE)
    n_devices = len({d.device_id for d in result.devices})

    lines.append("Power meter audit")
    lines.append("=" * 72)
    lines.append(
        f"Loaded: {len(result.activities)}  |  scored: {len(rides)}  |  "
        f"suspect: {n_suspect}  |  watch: {n_watch}  |  "
        f"quality issues: {n_quality} ({n_severe_q} severe)  |  devices: {n_devices}"
    )
    if result.baseline_bins:
        hr_range = f"{min(result.baseline_bins)}–{max(result.baseline_bins)} bpm"
        lines.append(f"Global baseline HR coverage: {hr_range} ({len(result.baseline_bins)} bins)")
    lines.append(f"Steady-state filter: {'on' if result.params.steady_state else 'off'}")
    lines.append("")

    # Ranking table
    lines.append("All rides (ranked by |ratio − 1|)")
    lines.append("-" * 72)
    header = (
        f"{'#':>3}  {_pad('name', 22)} {_pad('date', 10)} {_pad('ctx', 7)} "
        f"{_pad('device', 16)} {'ratio':>6} {'ΔW':>5} {'z':>5} {_pad('sev', 7)} flags"
    )
    lines.append(header)
    for i, r in enumerate(rides[:top], start=1):
        flags = ",".join(r.quality_flag_labels) if r.quality_flag_labels else "—"
        device = r.activity.power_device_label or r.activity.power_device_id
        lines.append(
            f"{i:3d}  {_pad(r.activity.name, 22)} {_pad(_fmt_date(r), 10)} "
            f"{_pad(r.activity.context_label, 7)} {_pad(device, 16)} "
            f"{r.mean_ratio:6.2f} {r.mean_delta_w:+5.0f} {r.z_score:5.1f} "
            f"{_pad(r.severity.value, 7)} {flags}"
        )
    if len(rides) > top:
        lines.append(f"  … {len(rides) - top} more rides omitted (raise --top)")
    lines.append("")

    # Suspect detail
    detail = result.findings[:top]
    if detail:
        lines.append("Suspect rides (detail)")
        lines.append("-" * 72)
        for i, finding in enumerate(detail, start=1):
            act = finding.activity
            when = act.start_time.isoformat(sep=" ", timespec="minutes") if act.start_time else "unknown date"
            lines.append(f"{i}. {finding.summary}")
            lines.append(
                f"   file: {act.path.name}  |  start: {when}  |  samples: {act.n_samples}  |  "
                f"baseline: {finding.baseline_source}  |  confidence: {finding.confidence:.2f}"
            )
            if act.mean_power is not None and act.mean_hr is not None:
                lines.append(
                    f"   ride means: {act.mean_power:.0f} W @ {act.mean_hr:.0f} bpm  |  "
                    f"duration: {act.duration_s / 60:.0f} min  |  device: {act.power_device_label}"
                )
            for example in finding.examples:
                lines.append(example)
            if verbose:
                lines.append("   all overlapping bins:")
                for d in finding.deviations:
                    lines.append(
                        f"     {d.hr_bin:3d} bpm  exp {d.expected_w:6.1f} W  "
                        f"obs {d.observed_w:6.1f} W  ratio {d.ratio:.3f}"
                    )
            lines.append("")
    else:
        lines.append("No suspect rides.")
        lines.append("")

    # Device comparison
    if result.devices:
        lines.append("Devices")
        lines.append("-" * 72)
        for g in result.devices:
            lines.append(
                f"- {g.label} [{g.device_id}]: {g.n_rides} rides, "
                f"median ratio {g.median_ratio:.2f}, mean ratio {g.mean_ratio:.2f}"
            )
        if result.device_comparisons:
            lines.append("")
            lines.append("Cross-device power at shared HR bins (median W)")
            # Collect device order
            device_ids = [g.device_id for g in result.devices]
            labels = {g.device_id: g.label for g in result.devices}
            short = {did: _pad(labels.get(did, did), 12) for did in device_ids}
            lines.append("  HR   " + "  ".join(short[d] for d in device_ids))
            for comp in result.device_comparisons[:15]:
                cells = []
                for did in device_ids:
                    if did in comp.devices:
                        cells.append(f"{comp.devices[did]:6.0f} W".ljust(12))
                    else:
                        cells.append("—".ljust(12))
                lines.append(f"  {comp.hr_bin:3d}  " + "  ".join(cells))
        lines.append("")

    # Quality
    if result.quality_findings:
        lines.append("Signal quality")
        lines.append("-" * 72)
        for f in sorted(result.quality_findings, key=lambda x: (x.severity.value != "severe", x.activity_name)):
            lines.append(f"- [{f.severity.value}] {f.activity_name} / {f.kind}: {f.description}")
        lines.append("")

    # Baseline curve
    if result.baseline_bins:
        lines.append("Global baseline (HR → typical watts)")
        lines.append("-" * 72)
        for b in sorted(result.baseline_bins):
            n = result.baseline_counts.get(b, 0)
            lines.append(f"  ~{b:3d} bpm → {result.baseline_bins[b]:6.1f} W  ({n} rides)")
        lines.append("")

    if verbose:
        lines.append("Per-ride HR-bin profiles")
        lines.append("-" * 72)
        for r in rides:
            bins = "  ".join(f"{b}:{r.profile.bins[b]:.0f}W" for b in sorted(r.profile.bins))
            lines.append(f"- {r.activity.name}: {bins or '(none)'}")
        lines.append("")

    if result.notes:
        lines.append("Notes")
        lines.append("-" * 72)
        skips = [n for n in result.notes if n.startswith("skipped")]
        other = [n for n in result.notes if not n.startswith("skipped")]
        for n in other:
            lines.append(f"- {n}")
        if skips:
            lines.append(f"- skipped {len(skips)} file(s) without usable power+HR pairs")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def result_to_dict(result: AuditResult) -> dict:
    def ride_dict(r: RideAssessment) -> dict:
        act = r.activity
        return {
            "file": str(act.path),
            "name": act.name,
            "start_time": act.start_time.isoformat() if act.start_time else None,
            "sport": act.sport,
            "sub_sport": act.sub_sport,
            "indoor": act.indoor,
            "has_gps": act.has_gps,
            "context": act.context_label,
            "device_id": act.power_device_id,
            "device_label": act.power_device_label,
            "manufacturer": act.power_manufacturer,
            "product": act.power_product,
            "n_samples": act.n_samples,
            "duration_s": act.duration_s,
            "mean_power": act.mean_power,
            "mean_hr": act.mean_hr,
            "score": r.score,
            "mean_ratio": r.mean_ratio,
            "median_ratio": r.median_ratio,
            "mean_delta_w": r.mean_delta_w,
            "covered_bins": r.covered_bins,
            "z_score": r.z_score,
            "confidence": r.confidence,
            "severity": r.severity.value,
            "baseline_source": r.baseline_source,
            "summary": r.summary,
            "profile_bins": {str(k): v for k, v in sorted(r.profile.bins.items())},
            "profile_counts": {str(k): v for k, v in sorted(r.profile.counts.items())},
            "deviations": [
                {
                    "hr_bin": d.hr_bin,
                    "expected_w": d.expected_w,
                    "observed_w": d.observed_w,
                    "delta_w": d.delta_w,
                    "ratio": d.ratio,
                    "baseline_n_activities": d.baseline_n_activities,
                    "activity_n_samples": d.activity_n_samples,
                }
                for d in r.deviations
            ],
            "quality": None
            if r.quality is None
            else {
                "dropout_rate": r.quality.dropout_rate,
                "stuck_fraction": r.quality.stuck_fraction,
                "spike_rate": r.quality.spike_rate,
                "zero_burst_fraction": r.quality.zero_burst_fraction,
                "flags": r.quality_flag_labels,
            },
        }

    return {
        "activity_count": len(result.activities),
        "scored_count": len(result.rides),
        "params": {
            "hr_bin_width": result.params.hr_bin_width,
            "min_bin_samples": result.params.min_bin_samples,
            "min_activities_per_bin": result.params.min_activities_per_bin,
            "min_overlap_bins": result.params.min_overlap_bins,
            "z_threshold": result.params.z_threshold,
            "min_ratio_shift": result.params.min_ratio_shift,
            "steady_state": result.params.steady_state,
            "cadence_min": result.params.cadence_min,
            "cadence_max": result.params.cadence_max,
            "context_match_min_rides": result.params.context_match_min_rides,
        },
        "baseline": {
            str(k): {"median_watts": v, "n_activities": result.baseline_counts.get(k, 0)}
            for k, v in sorted(result.baseline_bins.items())
        },
        "rides": [ride_dict(r) for r in result.rides],
        "suspects": [r.activity.name for r in result.findings],
        "watch": [r.activity.name for r in result.watch],
        "devices": [
            {
                "device_id": g.device_id,
                "label": g.label,
                "n_rides": g.n_rides,
                "median_ratio": g.median_ratio,
                "mean_ratio": g.mean_ratio,
                "baseline_bins": {str(k): v for k, v in sorted(g.baseline_bins.items())},
                "ride_names": g.ride_names,
            }
            for g in result.devices
        ],
        "device_comparisons": [
            {"hr_bin": c.hr_bin, "devices": c.devices} for c in result.device_comparisons
        ],
        "quality_findings": [
            {
                "activity_name": f.activity_name,
                "activity_path": f.activity_path,
                "kind": f.kind,
                "severity": f.severity.value,
                "description": f.description,
                "metric": f.metric,
            }
            for f in result.quality_findings
        ],
        "notes": result.notes,
    }
