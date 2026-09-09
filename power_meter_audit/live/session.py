"""Recorded session: raw samples plus the timeline they were captured against."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from power_meter_audit.live.protocol import TimelineSegment
from power_meter_audit.live.sources import PEDALS, TRAINER, Sample


@dataclass
class SessionLog:
    protocol_name: str
    timeline: list[TimelineSegment]
    samples: list[Sample] = field(default_factory=list)
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    cadence_tolerance_rpm: float = 5.0
    notes: list[str] = field(default_factory=list)

    def add(self, sample: Sample) -> None:
        self.samples.append(sample)

    def samples_for(self, source: str, start_s: float, end_s: float) -> list[Sample]:
        return [s for s in self.samples if s.source == source and start_s <= s.t < end_s]

    def source_names(self) -> list[str]:
        seen: list[str] = []
        for s in self.samples:
            if s.source not in seen:
                seen.append(s.source)
        return seen

    @property
    def duration_s(self) -> float:
        return max((s.t for s in self.samples), default=0.0)

    def to_dict(self) -> dict:
        return {
            "protocol_name": self.protocol_name,
            "started_at": self.started_at.isoformat(),
            "cadence_tolerance_rpm": self.cadence_tolerance_rpm,
            "notes": self.notes,
            "timeline": [
                {
                    "index": seg.index,
                    "step_index": seg.step_index,
                    "label": seg.label,
                    "target_watts": seg.target_watts,
                    "target_rpm": seg.target_rpm,
                    "start_s": seg.start_s,
                    "end_s": seg.end_s,
                    "measure_from_s": seg.measure_from_s,
                    "is_warmup": seg.is_warmup,
                }
                for seg in self.timeline
            ],
            "samples": [
                {"t": s.t, "source": s.source, "watts": s.watts, "cadence": s.cadence}
                for s in self.samples
            ],
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "SessionLog":
        timeline = [TimelineSegment(**seg) for seg in payload.get("timeline", [])]
        samples = [Sample(**s) for s in payload.get("samples", [])]
        started = payload.get("started_at")
        return cls(
            protocol_name=payload.get("protocol_name", "unknown"),
            timeline=timeline,
            samples=samples,
            started_at=datetime.fromisoformat(started) if started else datetime.now(timezone.utc),
            cadence_tolerance_rpm=payload.get("cadence_tolerance_rpm", 5.0),
            notes=payload.get("notes", []),
        )

    def save_json(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    @classmethod
    def load_json(cls, path: Path) -> "SessionLog":
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def save_csv(self, path: Path) -> None:
        """Wide CSV for spreadsheet use: one row per sample instant per source."""
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["t_s", "source", "watts", "cadence_rpm", "segment", "target_watts", "target_rpm"])
            by_time = sorted(self.samples, key=lambda s: (s.t, s.source))
            for sample in by_time:
                seg = self.segment_at(sample.t)
                writer.writerow(
                    [
                        f"{sample.t:.3f}",
                        sample.source,
                        "" if sample.watts is None else f"{sample.watts:.1f}",
                        "" if sample.cadence is None else f"{sample.cadence:.1f}",
                        seg.cell_label if seg else "",
                        seg.target_watts if seg else "",
                        seg.target_rpm if seg and seg.target_rpm is not None else "",
                    ]
                )

    def segment_at(self, t: float) -> TimelineSegment | None:
        for seg in self.timeline:
            if seg.start_s <= t < seg.end_s:
                return seg
        return None

    def yield_rate(self, source: str, expected_hz: float) -> float:
        """Fraction of expected samples actually received — catches ANT dropouts."""
        duration = self.duration_s
        if duration <= 0 or expected_hz <= 0:
            return 0.0
        received = sum(1 for s in self.samples if s.source == source and s.watts is not None)
        return min(1.0, received / (duration * expected_hz))


__all__ = ["SessionLog", "TRAINER", "PEDALS"]
