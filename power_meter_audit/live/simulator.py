"""Simulated trainer, pedals and rider.

Lets the protocol runner and the analysis be exercised end to end without a
bike attached, and lets known faults be injected so the analysis can be tested
against a ground truth it is not told about.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from power_meter_audit.live.sources import PEDALS, TRAINER, PowerSource, TrainerSource, VirtualClock


def _omega(cadence_rpm: float) -> float:
    return cadence_rpm * 2.0 * math.pi / 60.0


@dataclass
class MeterModel:
    """How a simulated meter deviates from true crank power.

    `torque_gain` is the interesting one: a fractional scale error proportional
    to crank torque, which is what a strain-gauge nonlinearity looks like and
    what a constant scale error does not.
    """

    scale: float = 1.0
    offset_w: float = 0.0
    torque_gain: float = 0.0
    drivetrain_loss: float = 0.0
    left_fraction: float | None = None
    cadence_scale: float = 1.0
    noise_w: float = 2.0
    dropout_rate: float = 0.0

    def report(
        self, true_watts: float, cadence_rpm: float, rng: random.Random
    ) -> tuple[float | None, float | None]:
        if self.dropout_rate and rng.random() < self.dropout_rate:
            return None, None

        base = true_watts
        if self.left_fraction is not None:
            base *= 2.0 * self.left_fraction
        base *= 1.0 - self.drivetrain_loss

        omega = _omega(cadence_rpm)
        torque = true_watts / omega if omega > 0.1 else 0.0
        factor = self.scale + self.torque_gain * torque

        watts = base * factor + self.offset_w + rng.gauss(0.0, self.noise_w)
        return max(0.0, watts), cadence_rpm * self.cadence_scale


@dataclass
class RiderModel:
    """A rider who holds the ERG power and chases the cadence guide imperfectly."""

    cadence_tau_s: float = 4.0
    cadence_bias_rpm: float = 0.0
    cadence_noise_rpm: float = 1.5
    power_tau_s: float = 3.0
    power_noise_w: float = 4.0
    start_cadence_rpm: float = 85.0


@dataclass
class SimulatedRig:
    """Owns the true crank power and cadence that both simulated meters observe."""

    trainer_model: MeterModel = field(
        default_factory=lambda: MeterModel(drivetrain_loss=0.025, noise_w=2.0)
    )
    pedal_model: MeterModel = field(
        default_factory=lambda: MeterModel(left_fraction=0.5, noise_w=3.0)
    )
    rider: RiderModel = field(default_factory=RiderModel)
    trainer_hz: float = 1.0
    pedal_hz: float = 4.0
    seed: int = 12345

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)
        self._target_watts = 0.0
        self._target_rpm: float | None = None
        self._true_watts = 0.0
        self._cadence = self.rider.start_cadence_rpm
        self._last_t = 0.0
        self._next_trainer = 0.0
        self._next_pedals = 0.0
        self.trainer = SimulatedTrainer(self)
        self.pedals = SimulatedPedals(self)

    def set_target_power(self, watts: float) -> None:
        self._target_watts = float(watts)

    def set_target_cadence(self, rpm: float | None) -> None:
        self._target_rpm = float(rpm) if rpm is not None else None

    def reset(self, t: float = 0.0) -> None:
        """Rewind the emission schedule so a new session starts from `t`.

        Without this, a rig that streamed during a connection preview has its
        next-sample times parked far in the future and emits nothing once the
        session clock restarts at zero.
        """
        self._last_t = t
        self._next_trainer = t
        self._next_pedals = t

    def tick(self, t: float) -> None:
        if t + 1e-9 < self._last_t:
            self.reset(t)
        dt = max(0.0, t - self._last_t)
        self._last_t = t
        if dt <= 0.0:
            return

        # ERG walks true power toward the commanded target.
        alpha_p = 1.0 - math.exp(-dt / max(self.rider.power_tau_s, 1e-6))
        self._true_watts += (self._target_watts - self._true_watts) * alpha_p

        goal = self._target_rpm if self._target_rpm is not None else self._cadence
        goal += self.rider.cadence_bias_rpm
        alpha_c = 1.0 - math.exp(-dt / max(self.rider.cadence_tau_s, 1e-6))
        self._cadence += (goal - self._cadence) * alpha_c

        # Emit every sample that fell due since the last tick, so the sample
        # rate stays 1 Hz / 4 Hz in session time no matter how coarsely (or how
        # fast) the driver ticks.
        self._next_trainer = self._drain(
            self._next_trainer, 1.0 / self.trainer_hz, t, self.trainer, self.trainer_model
        )
        self._next_pedals = self._drain(
            self._next_pedals, 1.0 / self.pedal_hz, t, self.pedals, self.pedal_model
        )

    def _drain(
        self,
        next_due: float,
        period: float,
        t: float,
        source: PowerSource,
        model: MeterModel,
        max_per_tick: int = 200,
    ) -> float:
        emitted = 0
        while next_due <= t and emitted < max_per_tick:
            watts = max(0.0, self._true_watts + self._rng.gauss(0.0, self.rider.power_noise_w))
            cadence = max(0.0, self._cadence + self._rng.gauss(0.0, self.rider.cadence_noise_rpm))
            reported_w, reported_rpm = model.report(watts, cadence, self._rng)
            source.emit(next_due, reported_w, reported_rpm)
            next_due += period
            emitted += 1
        return next_due if emitted < max_per_tick else t + period

    def attach(self, clock: VirtualClock) -> None:
        clock.add_tick_handler(self.tick)


class SimulatedTrainer(TrainerSource):
    def __init__(self, rig: SimulatedRig) -> None:
        super().__init__(TRAINER, "Simulated trainer")
        self._rig = rig

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def set_target_power(self, watts: int) -> None:
        self._rig.set_target_power(watts)


class SimulatedPedals(PowerSource):
    def __init__(self, rig: SimulatedRig) -> None:
        super().__init__(PEDALS, "Simulated pedals")
        self._rig = rig

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False
