"""Structured ERG test protocols for comparing two power sources.

A protocol is a ladder of ERG power steps. Each step is split into cadence
segments so the same wattage is ridden at two different crank torques — at a
fixed power, 70 rpm loads the drivetrain about 29% harder than 90 rpm, which
separates a torque-dependent error from a power-dependent one.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CadenceSegment:
    """One cadence hold within a power step."""

    target_rpm: int
    duration_s: int


@dataclass(frozen=True)
class Step:
    """One ERG power level, ridden at one or more cadences."""

    target_watts: int
    segments: tuple[CadenceSegment, ...]
    label: str = ""

    @property
    def duration_s(self) -> int:
        return sum(s.duration_s for s in self.segments)


@dataclass(frozen=True)
class TimelineSegment:
    """A power/cadence cell resolved onto the session clock."""

    index: int
    step_index: int
    label: str
    target_watts: int
    target_rpm: int | None
    start_s: float
    end_s: float
    measure_from_s: float
    is_warmup: bool = False

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s

    @property
    def measure_duration_s(self) -> float:
        return max(0.0, self.end_s - self.measure_from_s)

    @property
    def cell_label(self) -> str:
        if self.target_rpm is None:
            return self.label
        return f"{self.label} @{self.target_rpm}rpm"


@dataclass
class Protocol:
    """A full test session: optional warm-up followed by measured steps."""

    name: str
    steps: list[Step]
    warmup_s: int = 600
    warmup_watts: int = 120
    power_settle_s: float = 30.0
    cadence_settle_s: float = 15.0
    cadence_tolerance_rpm: float = 5.0
    notes: list[str] = field(default_factory=list)

    def timeline(self) -> list[TimelineSegment]:
        """Resolve the protocol onto absolute session times."""
        segments: list[TimelineSegment] = []
        t = 0.0
        index = 0

        if self.warmup_s > 0:
            segments.append(
                TimelineSegment(
                    index=index,
                    step_index=-1,
                    label="warm-up",
                    target_watts=self.warmup_watts,
                    target_rpm=None,
                    start_s=0.0,
                    end_s=float(self.warmup_s),
                    measure_from_s=float(self.warmup_s),
                    is_warmup=True,
                )
            )
            t = float(self.warmup_s)
            index += 1

        for step_index, step in enumerate(self.steps):
            for seg_index, segment in enumerate(step.segments):
                # A power change needs longer to settle than a cadence change,
                # because ERG has to walk the resistance to a new set point.
                settle = self.power_settle_s if seg_index == 0 else self.cadence_settle_s
                settle = min(settle, segment.duration_s * 0.5)
                end = t + segment.duration_s
                segments.append(
                    TimelineSegment(
                        index=index,
                        step_index=step_index,
                        label=step.label or f"step {step_index + 1}",
                        target_watts=step.target_watts,
                        target_rpm=segment.target_rpm,
                        start_s=t,
                        end_s=end,
                        measure_from_s=t + settle,
                    )
                )
                t = end
                index += 1

        return segments

    @property
    def total_duration_s(self) -> float:
        return float(self.warmup_s) + sum(s.duration_s for s in self.steps)

    @property
    def measured_segment_count(self) -> int:
        return sum(len(s.segments) for s in self.steps)


def _ladder(
    watts: list[int],
    cadences: tuple[int, ...],
    seconds_per_cadence: int,
    label: str = "",
) -> list[Step]:
    return [
        Step(
            target_watts=w,
            segments=tuple(CadenceSegment(rpm, seconds_per_cadence) for rpm in cadences),
            label=label or f"{w}W",
        )
        for w in watts
    ]


def standard_protocol(
    watts: list[int] | None = None,
    cadences: tuple[int, ...] = (70, 90),
    seconds_per_cadence: int = 120,
    warmup_s: int = 600,
) -> Protocol:
    """Full ladder plus a repeat of the opening step to expose session drift."""
    watts = watts or [150, 200, 250, 300, 350]
    steps = _ladder(watts, cadences, seconds_per_cadence)
    steps.append(
        Step(
            target_watts=watts[0],
            segments=tuple(CadenceSegment(rpm, seconds_per_cadence) for rpm in cadences),
            label=f"{watts[0]}W repeat",
        )
    )
    return Protocol(
        name="standard",
        steps=steps,
        warmup_s=warmup_s,
        notes=[
            "Run a Kickr spindown at the end of the warm-up, before the first step.",
            "Zero-offset the pedals from the head unit before starting.",
            "The repeated opening step measures drift across the session, not calibration.",
        ],
    )


def quick_protocol(
    watts: list[int] | None = None,
    cadences: tuple[int, ...] = (70, 90),
    seconds_per_cadence: int = 90,
    warmup_s: int = 300,
) -> Protocol:
    """Short version: three power levels, still two cadences each."""
    watts = watts or [150, 250, 350]
    return Protocol(
        name="quick",
        steps=_ladder(watts, cadences, seconds_per_cadence),
        warmup_s=warmup_s,
        notes=["Short session — expect wider confidence intervals per cell."],
    )


PRESETS = {
    "standard": standard_protocol,
    "quick": quick_protocol,
}
