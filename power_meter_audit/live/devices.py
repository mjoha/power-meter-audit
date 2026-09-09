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


def ensure_usb_backend() -> str:
    """Give pyusb a libusb backend if the platform does not supply one.

    openant calls ``usb.core.find()`` with no backend, so pyusb has to locate
    libusb by itself. Windows ships none, which surfaces as a bare "No backend
    available". ``libusb_package`` bundles the DLL but keeps it inside
    site-packages, where pyusb's library search will not look, so the backend is
    primed here from that path. pyusb caches it, and openant's later plain
    ``find()`` calls then succeed unmodified.
    """
    try:
        import usb.backend.libusb1
    except ImportError:
        return "pyusb-missing"

    if usb.backend.libusb1.get_backend() is not None:
        return "system"

    try:
        import libusb_package
    except ImportError:
        return "unavailable"

    path = libusb_package.get_library_path()
    if path is None:
        return "unavailable"
    backend = usb.backend.libusb1.get_backend(find_library=lambda _: str(path))
    return "bundled" if backend is not None else "unavailable"


def ant_error_hint(exc: BaseException) -> str:
    """Turn openant's opaque USB failures into something actionable.

    Two failures dominate on Windows and neither explains itself: pyusb raises
    "No backend available" when libusb is absent, and openant raises
    ``DriverNotFound`` with an empty message when libusb works but no stick is
    claimable.
    """
    name = type(exc).__name__
    text = str(exc).strip()

    if name == "NoBackendError" or "no backend available" in text.lower():
        return (
            "ANT+ needs libusb, which Windows does not ship. Install it with "
            "'pip install libusb-package', then bind the ANT+ stick to the WinUSB "
            "driver using Zadig (https://zadig.akeo.ie). Until then, use BLE for "
            "the pedals instead."
        )
    if name == "DriverNotFound":
        return (
            "libusb is working but no ANT+ stick could be claimed. Check it is "
            "plugged in, that Zadig has bound it to WinUSB rather than Garmin's "
            "own driver, and that Garmin Express or an ANT Agent is not holding it."
        )
    if name == "USBError" and ("access" in text.lower() or "permission" in text.lower()):
        return (
            "The ANT+ stick was found but could not be opened. Another program is "
            "probably holding it, or it is still bound to Garmin's driver rather "
            "than WinUSB."
        )
    return f"ANT+ setup failed: {text or name}"


class CrankCadenceTracker:
    """Derives cadence from Cycling Power Service crank revolution counters.

    The BLE Cycling Power Measurement carries no cadence field. It reports a
    cumulative crank count and the time of the last crank event in 1/1024 s,
    both 16-bit and both free to wrap, and cadence is the ratio of their deltas.
    """

    ROLLOVER = 1 << 16
    TIME_UNIT_S = 1.0 / 1024.0
    MAX_PLAUSIBLE_RPM = 250.0

    def __init__(self, coast_timeout_s: float = 3.0) -> None:
        self.coast_timeout_s = coast_timeout_s
        self._last_revs: int | None = None
        self._last_event: int | None = None
        self._last_change_t: float | None = None
        self._cadence: float | None = None

    def update(self, revs: int | None, event_time: int | None, now: float) -> float | None:
        if revs is None or event_time is None:
            return None

        if self._last_revs is None or self._last_event is None:
            self._last_revs, self._last_event, self._last_change_t = revs, event_time, now
            return None

        d_revs = (revs - self._last_revs) % self.ROLLOVER
        d_ticks = (event_time - self._last_event) % self.ROLLOVER

        if d_ticks == 0:
            # No crank event since the last notification. That is normal between
            # updates at low cadence, but a long silence means coasting.
            if self._last_change_t is not None and now - self._last_change_t >= self.coast_timeout_s:
                self._cadence = 0.0
            return self._cadence

        self._last_revs, self._last_event, self._last_change_t = revs, event_time, now
        cadence = d_revs / (d_ticks * self.TIME_UNIT_S) * 60.0
        if cadence > self.MAX_PLAUSIBLE_RPM:
            return self._cadence
        self._cadence = cadence
        return cadence


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

        # Clearing whatever Zwift left behind is only a nicety, so a machine
        # that refuses Reset should still be usable. Reset also returns the
        # machine to its default state, which on some firmwares drops the
        # control permission just granted, so take it again afterwards.
        try:
            await self._ftms.reset()
        except Exception:  # noqa: BLE001 - optional tidy-up
            pass
        else:
            await self._ftms.request_control()

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
        ensure_usb_backend()
        try:
            self._node = Node()
        except Exception as exc:  # noqa: BLE001 - re-raised with a usable message
            raise RuntimeError(ant_error_hint(exc)) from exc
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

    POWER_FIELDS = ("instantaneous_power", "instant_power", "power")

    def __init__(self, address: str, clock: Clock, label: str = "Pedals") -> None:
        super().__init__(PEDALS, label)
        self.address = address
        self._clock = clock
        self._client = None
        self._cadence = CrankCadenceTracker()

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
        watts = _first_attr(data, self.POWER_FIELDS)
        now = self._clock.now()
        cadence = self._cadence.update(
            _first_attr(data, ("cumulative_crank_revs",)),
            _first_attr(data, ("last_crank_event_time",)),
            now,
        )
        self.emit(now, float(watts) if watts is not None else None, cadence)

    async def disconnect(self) -> None:
        if self._client is not None:
            await self._client.disconnect()
        self.connected = False


async def scan(timeout: float = 8.0) -> list[dict[str, Any]]:
    """Discover nearby BLE devices, flagging those advertising power or FTMS.

    Everything named is returned rather than only recognised devices. Some
    trainers advertise no service UUIDs until you connect, and filtering those
    out leaves the one device you need invisible and unselectable.
    """
    from bleak import BleakScanner

    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    results: list[dict[str, Any]] = []
    for device, adv in found.values():
        uuids = {u.lower() for u in (adv.service_uuids or [])}
        is_trainer = FTMS_SERVICE_UUID in uuids
        is_power = CYCLING_POWER_SERVICE_UUID in uuids
        name = device.name or getattr(adv, "local_name", None)
        if not (name or is_trainer or is_power):
            continue
        results.append(
            {
                "address": device.address,
                "name": name or "unknown",
                "trainer": is_trainer,
                "power": is_power,
                "rssi": getattr(adv, "rssi", None),
            }
        )
    results.sort(key=lambda d: (not d["trainer"], not d["power"], -(d["rssi"] or -999)))
    return results
