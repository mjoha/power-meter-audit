"""Drives a protocol: commands ERG targets, guides cadence, records both streams."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Callable

from power_meter_audit.live.protocol import Protocol, TimelineSegment
from power_meter_audit.live.session import SessionLog
from power_meter_audit.live.sources import Clock, PowerSource, RealClock, Sample, TrainerSource


@dataclass
class SessionStarted:
    protocol_name: str
    total_duration_s: float


@dataclass
class SegmentStarted:
    segment: TimelineSegment


@dataclass
class SegmentFinished:
    segment: TimelineSegment


@dataclass
class LiveStatus:
    """Everything a UI needs to render the run screen."""

    t: float
    segment: TimelineSegment
    elapsed_s: float
    remaining_s: float
    measuring: bool
    trainer: Sample | None
    pedals: Sample | None
    cadence_target: int | None
    cadence_error: float | None
    cadence_in_zone: bool


@dataclass
class ControlWarning:
    message: str


@dataclass
class SessionFinished:
    log: SessionLog


SessionEvent = (
    SessionStarted | SegmentStarted | SegmentFinished | LiveStatus | ControlWarning | SessionFinished
)
EventHandler = Callable[[SessionEvent], None]


class SessionRunner:
    def __init__(
        self,
        protocol: Protocol,
        trainer: TrainerSource,
        pedals: PowerSource,
        clock: Clock | None = None,
        on_event: EventHandler | None = None,
        status_interval_s: float = 0.5,
        control_retries: int = 3,
        erg_tolerance: float = 0.15,
        erg_grace_s: float = 20.0,
    ) -> None:
        self.protocol = protocol
        self.trainer = trainer
        self.pedals = pedals
        self.clock = clock or RealClock()
        self.on_event = on_event
        self.status_interval_s = status_interval_s
        self.control_retries = control_retries
        self.erg_tolerance = erg_tolerance
        self.erg_grace_s = erg_grace_s
        self._stop = False
        self._recent_trainer: deque[tuple[float, float]] = deque(maxlen=600)

    def stop(self) -> None:
        self._stop = True

    def _emit(self, event: SessionEvent) -> None:
        if self.on_event:
            self.on_event(event)

    async def _set_target(self, watts: int) -> None:
        # FTMS control-point writes fail intermittently on both BlueZ and WinRT
        # with vendor protocol errors; retrying is the accepted workaround.
        for attempt in range(1, self.control_retries + 1):
            try:
                await self.trainer.set_target_power(watts)
                return
            except Exception as exc:  # noqa: BLE001 - transport-agnostic retry
                self._emit(
                    ControlWarning(
                        f"set_target_power({watts}) failed on attempt {attempt}/{self.control_retries}: {exc}"
                    )
                )
                if attempt == self.control_retries:
                    raise
                await self.clock.sleep(0.5)

    async def run(self) -> SessionLog:
        timeline = self.protocol.timeline()
        log = SessionLog(
            protocol_name=self.protocol.name,
            timeline=timeline,
            cadence_tolerance_rpm=self.protocol.cadence_tolerance_rpm,
            notes=list(self.protocol.notes),
        )

        self.trainer.add_handler(log.add)
        self.trainer.add_handler(self._track_trainer)
        self.pedals.add_handler(log.add)

        # The UI already connected these to verify the radios. Reconnecting a
        # BLE trainer is exclusive and tears down the working link, which is
        # how Start produced "Characteristic 0x2AD2 was not found" while both
        # sources were already streaming. Only take (and later drop) a link
        # this runner opened itself; the CLI still does both.
        self._stop = False
        self._recent_trainer.clear()
        took_trainer = False
        took_pedals = False
        if not self.trainer.connected:
            await self.trainer.connect()
            took_trainer = True
        if not self.pedals.connected:
            await self.pedals.connect()
            took_pedals = True
        self._emit(SessionStarted(self.protocol.name, self.protocol.total_duration_s))

        try:
            commanded: int | None = None
            for segment in timeline:
                if self._stop:
                    break
                if segment.target_watts != commanded:
                    await self._set_target(segment.target_watts)
                    commanded = segment.target_watts
                self._emit(SegmentStarted(segment))
                await self._ride_segment(segment)
                self._emit(SegmentFinished(segment))
        finally:
            if took_trainer:
                await self.trainer.disconnect()
            if took_pedals:
                await self.pedals.disconnect()

        self._emit(SessionFinished(log))
        return log

    def _track_trainer(self, sample: Sample) -> None:
        if sample.watts is not None:
            self._recent_trainer.append((sample.t, sample.watts))

    def _check_erg_hold(self, segment: TimelineSegment, now: float) -> bool:
        """Warn if the trainer is not actually holding the commanded target.

        A control-point write that is accepted and then ignored raises nothing,
        so without this an entire session can be ridden at the wrong resistance
        with no indication that ERG never engaged.
        """
        window = [w for t, w in self._recent_trainer if t >= now - self.erg_grace_s]
        if len(window) < 5 or segment.target_watts <= 0:
            return False

        mean = sum(window) / len(window)
        if abs(mean - segment.target_watts) / segment.target_watts <= self.erg_tolerance:
            return False

        self._emit(
            ControlWarning(
                f"{segment.cell_label}: trainer is holding {mean:.0f} W against a "
                f"{segment.target_watts} W target, so ERG does not look engaged. "
                "Check no other app holds the trainer and that it granted control."
            )
        )
        return True

    async def _ride_segment(self, segment: TimelineSegment) -> None:
        end = segment.end_s
        warned = False
        while not self._stop:
            now = self.clock.now()
            if now >= end:
                break
            self._emit(self._status(now, segment))
            if not warned and now - segment.measure_from_s >= self.erg_grace_s:
                warned = self._check_erg_hold(segment, now)
            await self.clock.sleep(min(self.status_interval_s, end - now))

    def _status(self, now: float, segment: TimelineSegment) -> LiveStatus:
        trainer_sample = self.trainer.last_sample
        pedal_sample = self.pedals.last_sample

        cadence_error: float | None = None
        in_zone = True
        if segment.target_rpm is not None:
            observed = trainer_sample.cadence if trainer_sample else None
            if observed is None:
                in_zone = False
            else:
                cadence_error = observed - segment.target_rpm
                in_zone = abs(cadence_error) <= self.protocol.cadence_tolerance_rpm

        return LiveStatus(
            t=now,
            segment=segment,
            elapsed_s=now - segment.start_s,
            remaining_s=max(0.0, segment.end_s - now),
            measuring=now >= segment.measure_from_s,
            trainer=trainer_sample,
            pedals=pedal_sample,
            cadence_target=segment.target_rpm,
            cadence_error=cadence_error,
            cadence_in_zone=in_zone,
        )
