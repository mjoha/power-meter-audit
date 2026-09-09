"""Power source abstraction shared by real devices and the simulator.

Every sample is stamped with the session clock at the moment the host receives
it, so both streams share one clock. That is the whole reason a live capture
beats aligning two FIT files after the fact.
"""

from __future__ import annotations

import abc
import asyncio
import time
from dataclasses import dataclass
from typing import Callable

SampleHandler = Callable[["Sample"], None]

TRAINER = "trainer"
PEDALS = "pedals"


@dataclass(frozen=True)
class Sample:
    t: float
    source: str
    watts: float | None
    cadence: float | None


class Clock(abc.ABC):
    """Session clock. Injectable so protocols can run in simulated time."""

    @abc.abstractmethod
    def now(self) -> float:
        ...

    @abc.abstractmethod
    async def sleep(self, seconds: float) -> None:
        ...


class ScaledClock(Clock):
    """Wall clock, optionally accelerated.

    A speed above 1 lets a 45-minute protocol be exercised end to end in
    seconds, which is the only practical way to test the UI without a bike.
    """

    def __init__(self, speed: float = 1.0) -> None:
        self.speed = max(float(speed), 1e-6)
        self._t0 = time.monotonic()

    def now(self) -> float:
        return (time.monotonic() - self._t0) * self.speed

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds / self.speed)


class RealClock(ScaledClock):
    def __init__(self) -> None:
        super().__init__(1.0)


class VirtualClock(Clock):
    """Advances instantly, driving tick handlers so a 40-minute protocol runs in
    milliseconds. Simulated devices attach here to emit samples."""

    def __init__(self, step_s: float = 0.25) -> None:
        self._t = 0.0
        self.step_s = step_s
        self._ticks: list[Callable[[float], None]] = []

    def now(self) -> float:
        return self._t

    def add_tick_handler(self, handler: Callable[[float], None]) -> None:
        self._ticks.append(handler)

    async def sleep(self, seconds: float) -> None:
        target = self._t + seconds
        while self._t < target - 1e-9:
            self._t = min(target, self._t + self.step_s)
            for handler in list(self._ticks):
                handler(self._t)
            await asyncio.sleep(0)


class PowerSource(abc.ABC):
    """A device that reports power and cadence."""

    def __init__(self, name: str, label: str = "") -> None:
        self.name = name
        self.label = label or name
        self._handlers: list[SampleHandler] = []
        self.connected = False
        self.last_sample: Sample | None = None

    def add_handler(self, handler: SampleHandler) -> None:
        self._handlers.append(handler)

    def emit(self, t: float, watts: float | None, cadence: float | None) -> None:
        sample = Sample(t=t, source=self.name, watts=watts, cadence=cadence)
        self.last_sample = sample
        for handler in self._handlers:
            handler(sample)

    @abc.abstractmethod
    async def connect(self) -> None:
        ...

    @abc.abstractmethod
    async def disconnect(self) -> None:
        ...


class TrainerSource(PowerSource):
    """A power source that also accepts ERG target commands."""

    @abc.abstractmethod
    async def set_target_power(self, watts: int) -> None:
        ...
