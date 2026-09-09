"""Adapters for real hardware: a Wahoo trainer over BLE FTMS, pedals over ANT+.

Imports are deferred so the rest of the package works on a machine with no
radios and no optional dependencies installed.

NOTE: these adapters have not been run against physical hardware. The protocol
sequences follow the FTMS spec and the pycycling/openant APIs, but field names
vary between library versions, so they read values defensively.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

from power_meter_audit.live.sources import PEDALS, TRAINER, Clock, PowerSource, TrainerSource

FTMS_SERVICE_UUID = "00001826-0000-1000-8000-00805f9b34fb"
CYCLING_POWER_SERVICE_UUID = "00001818-0000-1000-8000-00805f9b34fb"


def _first_attr(obj: Any, names: tuple[str, ...]) -> Any:
    for name in names:
        value = getattr(obj, name, None)
        if value is not None:
            return value
    if isinstance(obj, dict):
        for name in names:
            if obj.get(name) is not None:
                return obj[name]
    return None


class FtmsTrainer(TrainerSource):
    """Wahoo Kickr (and any FTMS trainer) over Bluetooth LE."""

    POWER_FIELDS = ("instant_power", "instantaneous_power", "power")
    CADENCE_FIELDS = ("instant_cadence", "instantaneous_cadence", "cadence")

    def __init__(self, address: str, clock: Clock, label: str = "Kickr") -> None:
        super().__init__(TRAINER, label)
        self.address = address
        self._clock = clock
        self._client = None
        self._ftms = None

    async def connect(self) -> None:
        from bleak import BleakClient
        from pycycling.fitness_machine_service import FitnessMachineService

        self._client = BleakClient(self.address)
        await self._client.connect()
        self._ftms = FitnessMachineService(self._client)

        self._ftms.set_indoor_bike_data_handler(self._on_indoor_bike_data)
        await self._ftms.enable_indoor_bike_data_notify()
        # Indications on the control point must be enabled before any write, and
        # control must be requested before any target command, or writes fail.
        await self._ftms.enable_control_point_indicate()
        await self._ftms.request_control()
        await self._ftms.reset()
        self.connected = True

    def _on_indoor_bike_data(self, data: Any) -> None:
        watts = _first_attr(data, self.POWER_FIELDS)
        cadence = _first_attr(data, self.CADENCE_FIELDS)
        self.emit(
            self._clock.now(),
            float(watts) if watts is not None else None,
            float(cadence) if cadence is not None else None,
        )

    async def set_target_power(self, watts: int) -> None:
        if self._ftms is None:
            raise RuntimeError("trainer not connected")
        await self._ftms.set_target_power(int(watts))

    async def disconnect(self) -> None:
        if self._ftms is not None:
            try:
                await self._ftms.reset()
            except Exception:  # noqa: BLE001 - best effort on teardown
                pass
        if self._client is not None:
            await self._client.disconnect()
        self.connected = False


class AntPlusPedals(PowerSource):
    """Power meter pedals over ANT+.

    ANT+ is preferred over BLE here because the broadcast supports unlimited
    listeners, so the head unit can keep recording the same ride while this
    captures it independently. The Rally only allows a couple of concurrent BLE
    connections, and Garmin's own dropout advice is to unpair everything else.
    """

    POWER_FIELDS = ("instantaneous_power", "instant_power", "power")
    CADENCE_FIELDS = ("cadence", "instantaneous_cadence")

    def __init__(self, clock: Clock, device_id: int = 0, label: str = "Pedals") -> None:
        super().__init__(PEDALS, label)
        self._clock = clock
        self.device_id = device_id
        self._node = None
        self._device = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    async def connect(self) -> None:
        from openant.devices import ANTPLUS_NETWORK_KEY
        from openant.devices.power_meter import PowerMeter
        from openant.easy.node import Node

        self._loop = asyncio.get_running_loop()
        self._node = Node()
        self._node.set_network_key(0x00, ANTPLUS_NETWORK_KEY)
        self._device = PowerMeter(self._node, device_id=self.device_id)
        self._device.on_device_data = self._on_device_data

        # openant is blocking, so the ANT node owns a thread and marshals samples
        # back onto the event loop.
        self._thread = threading.Thread(target=self._run_node, daemon=True)
        self._thread.start()
        self.connected = True

    def _run_node(self) -> None:
        try:
            self._node.start()
        except Exception:  # noqa: BLE001 - surfaced via absent samples
            pass

    def _on_device_data(self, page: Any, page_name: Any, data: Any) -> None:
        watts = _first_attr(data, self.POWER_FIELDS)
        if watts is None:
            return
        cadence = _first_attr(data, self.CADENCE_FIELDS)
        t = self._clock.now()

        def deliver() -> None:
            self.emit(t, float(watts), float(cadence) if cadence is not None else None)

        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(deliver)
        else:
            deliver()

    async def disconnect(self) -> None:
        if self._device is not None:
            try:
                self._device.close_channel()
            except Exception:  # noqa: BLE001 - best effort on teardown
                pass
        if self._node is not None:
            try:
                self._node.stop()
            except Exception:  # noqa: BLE001 - best effort on teardown
                pass
        self.connected = False


class BlePedals(PowerSource):
    """Fallback: pedals over the standard BLE Cycling Power Service.

    Uses one of the pedals' scarce BLE connection slots, so prefer ANT+ when a
    dongle is available.
    """

    def __init__(self, address: str, clock: Clock, label: str = "Pedals") -> None:
        super().__init__(PEDALS, label)
        self.address = address
        self._clock = clock
        self._client = None

    async def connect(self) -> None:
        from bleak import BleakClient
        from pycycling.cycling_power_service import CyclingPowerService

        self._client = BleakClient(self.address)
        await self._client.connect()
        self._cps = CyclingPowerService(self._client)
        self._cps.set_cycling_power_measurement_handler(self._on_measurement)
        await self._cps.enable_cycling_power_measurement_notifications()
        self.connected = True

    def _on_measurement(self, data: Any) -> None:
        watts = _first_attr(data, ("instantaneous_power", "instant_power", "power"))
        cadence = _first_attr(data, ("cadence", "instantaneous_cadence"))
        self.emit(
            self._clock.now(),
            float(watts) if watts is not None else None,
            float(cadence) if cadence is not None else None,
        )

    async def disconnect(self) -> None:
        if self._client is not None:
            await self._client.disconnect()
        self.connected = False


async def scan(timeout: float = 8.0) -> list[tuple[str, str]]:
    """Discover BLE devices advertising power or fitness-machine services."""
    from bleak import BleakScanner

    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    results: list[tuple[str, str]] = []
    for device, adv in found.values():
        uuids = {u.lower() for u in (adv.service_uuids or [])}
        if FTMS_SERVICE_UUID in uuids or CYCLING_POWER_SERVICE_UUID in uuids:
            results.append((device.address, device.name or "unknown"))
    return results
