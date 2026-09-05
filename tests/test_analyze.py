"""Unit tests using synthetic activities (no real FIT files required)."""

from __future__ import annotations

import json
from pathlib import Path

from power_meter_audit.analyze import Severity, audit_activities
from power_meter_audit.fit_loader import Activity
from power_meter_audit.html_report import write_html_report
from power_meter_audit.quality import QualitySeverity, analyze_signal_quality
from power_meter_audit.report import format_text_report, result_to_dict


def _make_activity(
    name: str,
    hr_power: list[tuple[float, float]],
    n_per: int = 40,
    *,
    indoor: bool = False,
    device_id: str = "unknown",
    device_label: str = "unknown",
    cadence: float | None = 90.0,
) -> Activity:
    power: list[float] = []
    hr: list[float] = []
    cad: list[float | None] = []
    for h, p in hr_power:
        power.extend([p] * n_per)
        hr.extend([h] * n_per)
        cad.extend([cadence] * n_per)
    return Activity(
        path=Path(f"{name}.fit"),
        name=name,
        start_time=None,
        sport="cycling",
        power=power,
        hr=hr,
        cadence=cad,
        speed=[None] * len(power),
        raw_power=list(power),
        raw_hr=list(hr),
        raw_timestamps=[None] * len(power),
        indoor=indoor,
        has_gps=not indoor,
        power_device_id=device_id,
        power_device_label=device_label,
    )


NORMAL_CURVE = [(130, 180), (140, 200), (150, 220), (160, 240), (170, 260)]


def test_flags_scaled_power_meter():
    activities = [_make_activity(f"normal_{i}", NORMAL_CURVE) for i in range(4)]
    bad_curve = [(h, p * 1.6) for h, p in NORMAL_CURVE]
    activities.append(_make_activity("suspect_high", bad_curve))

    result = audit_activities(activities, min_bin_samples=20, min_overlap_bins=3, z_threshold=1.0)
    names = [f.activity.name for f in result.findings]
    assert "suspect_high" in names
    suspect = next(f for f in result.findings if f.activity.name == "suspect_high")
    assert suspect.mean_ratio > 1.4
    assert suspect.severity == Severity.SUSPECT


def test_flags_low_reading_meter():
    activities = [_make_activity(f"normal_{i}", NORMAL_CURVE) for i in range(5)]
    low_curve = [(h, p * 0.7) for h, p in NORMAL_CURVE]
    activities.append(_make_activity("suspect_low", low_curve))

    result = audit_activities(activities, min_bin_samples=20, z_threshold=1.0)
    names = [f.activity.name for f in result.findings]
    assert "suspect_low" in names
    suspect = next(f for f in result.findings if f.activity.name == "suspect_low")
    assert suspect.mean_ratio < 0.85


def test_scores_all_rides():
    activities = [_make_activity(f"normal_{i}", NORMAL_CURVE) for i in range(4)]
    activities.append(_make_activity("high", [(h, p * 1.5) for h, p in NORMAL_CURVE]))
    result = audit_activities(activities, z_threshold=1.0)
    assert len(result.rides) == 5
    assert abs(result.rides[0].mean_ratio - 1.0) >= abs(result.rides[-1].mean_ratio - 1.0)


def test_device_grouping_and_comparison():
    a_curve = NORMAL_CURVE
    b_curve = [(h, p * 1.25) for h, p in NORMAL_CURVE]
    activities = [
        _make_activity(f"a{i}", a_curve, device_id="dev:a:1", device_label="Meter A") for i in range(3)
    ] + [
        _make_activity(f"b{i}", b_curve, device_id="dev:b:2", device_label="Meter B") for i in range(3)
    ]
    result = audit_activities(activities, z_threshold=1.0, min_ratio_shift=0.10)
    assert len(result.devices) >= 2
    ids = {d.device_id for d in result.devices}
    assert "dev:a:1" in ids and "dev:b:2" in ids
    assert result.device_comparisons
    # Meter B should read higher watts at shared bins
    sample = result.device_comparisons[len(result.device_comparisons) // 2]
    assert sample.devices["dev:b:2"] > sample.devices["dev:a:1"]


def test_quality_detects_stuck_and_spikes():
    act = _make_activity("noisy", NORMAL_CURVE)
    # Force stuck stretch and spikes in raw series
    act.raw_power = [200.0] * 40 + [200.0] * 30 + [100.0, 550.0, 120.0, 600.0] + [180.0] * 20
    act.raw_hr = [140.0] * len(act.raw_power)
    qm = analyze_signal_quality(act, stuck_seconds=20, spike_watts=400)
    kinds = {f.kind for f in qm.findings}
    assert "stuck_power" in kinds
    assert "spikes" in kinds


def test_quality_detects_dropouts():
    act = _make_activity("drop", NORMAL_CURVE)
    act.raw_power = [200.0 if i % 3 else None for i in range(90)]
    act.raw_hr = [140.0] * 90
    qm = analyze_signal_quality(act)
    assert any(f.kind == "dropout" for f in qm.findings)
    assert qm.dropout_rate > 0.2


def test_indoor_outdoor_context_baseline():
    outdoor = [_make_activity(f"out_{i}", NORMAL_CURVE, indoor=False) for i in range(3)]
    indoor_normal = [_make_activity(f"in_{i}", NORMAL_CURVE, indoor=True) for i in range(3)]
    indoor_high = _make_activity("in_high", [(h, p * 1.55) for h, p in NORMAL_CURVE], indoor=True)
    result = audit_activities(outdoor + indoor_normal + [indoor_high], z_threshold=1.0)
    high = next(r for r in result.rides if r.activity.name == "in_high")
    assert high.baseline_source in {"indoor", "global_fallback", "global"}
    assert high.severity in {Severity.SUSPECT, Severity.WATCH}


def test_json_and_html_smoke(tmp_path: Path):
    activities = [_make_activity(f"normal_{i}", NORMAL_CURVE) for i in range(4)]
    activities.append(_make_activity("suspect_high", [(h, p * 1.6) for h, p in NORMAL_CURVE]))
    result = audit_activities(activities, z_threshold=1.0)
    payload = result_to_dict(result)
    assert "rides" in payload and len(payload["rides"]) == 5
    assert "devices" in payload and "quality_findings" in payload
    text = format_text_report(result)
    assert "All rides" in text
    assert "suspect_high" in text

    html_path = tmp_path / "report.html"
    write_html_report(result, html_path)
    html = html_path.read_text(encoding="utf-8")
    assert "chartProfile" in html
    assert "Power meter audit" in html

    json_path = tmp_path / "report.json"
    json_path.write_text(json.dumps(payload), encoding="utf-8")
    assert json.loads(json_path.read_text())["scored_count"] == 5


def test_severe_quality_in_audit_result():
    activities = [_make_activity(f"normal_{i}", NORMAL_CURVE) for i in range(3)]
    bad = _make_activity("bad_q", NORMAL_CURVE)
    bad.raw_power = [None if i % 2 == 0 else 200.0 for i in range(100)]
    bad.raw_hr = [140.0] * 100
    activities.append(bad)
    result = audit_activities(activities, z_threshold=1.0)
    assert any(f.severity == QualitySeverity.SEVERE for f in result.quality_findings)
