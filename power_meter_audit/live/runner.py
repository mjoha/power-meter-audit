"""Drives a protocol: commands ERG targets, guides cadence, records both streams."""

from __future__ import annotations

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
    ) -> None:
        self.protocol = protocol
        self.trainer = trainer
        self.pedals = pedals
        self.clock = clock or RealClock()
        self.on_event = on_event
        self.status_interval_s = status_interval_s
        self.control_retries = control_retries
        self._stop = False

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
        self.pedals.add_handler(log.add)

        await self.trainer.connect()
        await self.pedals.connect()
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
            await self.trainer.disconnect()
            await self.pedals.disconnect()

        self._emit(SessionFinished(log))
        return log

    async def _ride_segment(self, segment: TimelineSegment) -> None:
        end = segment.end_s
        while not self._stop:
            now = self.clock.now()
            if now >= end:
                break
            self._emit(self._status(now, segment))
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
