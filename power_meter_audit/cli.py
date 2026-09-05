"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from power_meter_audit import __version__
from power_meter_audit.analyze import audit_activities
from power_meter_audit.fit_loader import load_directory
from power_meter_audit.html_report import write_html_report
from power_meter_audit.quality import QualitySeverity
from power_meter_audit.report import format_text_report, result_to_dict


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="power-meter-audit",
        description=(
            "Scan a directory of cycling .fit files and flag activities whose "
            "power readings look inconsistent for the same heart rate — a signal "
            "of power-meter calibration or hardware issues."
        ),
    )
    p.add_argument("directory", type=Path, help="Directory containing .fit files")
    p.add_argument("--no-recursive", action="store_true", help="Do not scan subdirectories")
    p.add_argument("--hr-bin", type=int, default=5, metavar="BPM", help="Heart-rate bin width in bpm (default: 5)")
    p.add_argument("--min-bin-samples", type=int, default=20, help="Minimum samples in an HR bin within one activity")
    p.add_argument("--min-overlap-bins", type=int, default=3, help="Minimum shared HR bins vs baseline to score a ride")
    p.add_argument("--min-ratio-shift", type=float, default=0.15, help="Minimum |power/expected - 1| for watch/suspect")
    p.add_argument("--z-threshold", type=float, default=1.5, help="Score z-score threshold for suspect rides")
    p.add_argument(
        "--no-steady-state",
        action="store_true",
        help="Disable cadence-based steady-state filtering",
    )
    p.add_argument("--json", type=Path, metavar="FILE", help="Write a full JSON report to FILE")
    p.add_argument("--html", type=Path, metavar="FILE", help="Write a self-contained HTML report to FILE")
    p.add_argument("--top", type=int, default=50, help="Max rides to show in ranking/detail (default: 50)")
    p.add_argument("--verbose", action="store_true", help="Include full HR-bin tables in the text report")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    directory: Path = args.directory
    if not directory.is_dir():
        print(f"error: not a directory: {directory}", file=sys.stderr)
        return 2

    activities, skip_notes, _stats = load_directory(directory, recursive=not args.no_recursive)
    result = audit_activities(
        activities,
        hr_bin_width=args.hr_bin,
        min_bin_samples=args.min_bin_samples,
        min_overlap_bins=args.min_overlap_bins,
        min_ratio_shift=args.min_ratio_shift,
        z_threshold=args.z_threshold,
        steady_state=not args.no_steady_state,
    )
    result.notes = skip_notes + result.notes

    print(format_text_report(result, top=args.top, verbose=args.verbose))

    if args.json:
        payload = result_to_dict(result)
        args.json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"Wrote JSON report to {args.json}")

    if args.html:
        write_html_report(result, args.html)
        print(f"Wrote HTML report to {args.html}")

    has_suspect = bool(result.findings)
    has_severe_quality = any(f.severity == QualitySeverity.SEVERE for f in result.quality_findings)
    return 0 if (has_suspect or has_severe_quality) else 1


if __name__ == "__main__":
    raise SystemExit(main())
