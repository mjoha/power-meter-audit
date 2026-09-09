"""Wiring helpers that assemble a runnable session from its parts."""

from __future__ import annotations

from typing import Callable

from power_meter_audit.live.protocol import Protocol
from power_meter_audit.live.runner import EventHandler, SegmentStarted, SessionRunner
from power_meter_audit.live.session import SessionLog
from power_meter_audit.live.simulator import SimulatedRig
from power_meter_audit.live.sources import Clock, PowerSource, TrainerSource, VirtualClock


def build_runner(
    protocol: Protocol,
    trainer: TrainerSource,
    pedals: PowerSource,
    clock: Clock,
    on_event: EventHandler | None = None,
    extra_segment_hook: Callable[[int | None], None] | None = None,
) -> SessionRunner:
    def handle(event) -> None:
        if extra_segment_hook is not None and isinstance(event, SegmentStarted):
            extra_segment_hook(event.segment.target_rpm)
        if on_event is not None:
            on_event(event)

    return SessionRunner(protocol, trainer, pedals, clock=clock, on_event=handle)


async def run_simulated_session(
    protocol: Protocol,
    rig: SimulatedRig | None = None,
    clock: VirtualClock | None = None,
    on_event: EventHandler | None = None,
) -> SessionLog:
    """Run a whole protocol in simulated time against a synthetic rig."""
    clock = clock or VirtualClock()
    rig = rig or SimulatedRig()
    rig.attach(clock)

    runner = build_runner(
        protocol,
        rig.trainer,
        rig.pedals,
        clock=clock,
        on_event=on_event,
        extra_segment_hook=rig.set_target_cadence,
    )
    return await runner.run()
