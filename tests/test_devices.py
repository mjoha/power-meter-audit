"""Tests for the hardware adapters' pure logic.

The radio paths cannot be exercised without a trainer, but the parts that
actually interpret device data can be, and those are where the bugs hide.
`devices` defers every optional import, so this module loads without bleak,
pycycling or openant present.
"""

from __future__ import annotations

import sys
import types

import pytest

from power_meter_audit.live.devices import (
    AntPlusPedals,
    BlePedals,
    CrankCadenceTracker,
    FtmsTrainer,
    _first_attr,
    ant_error_hint,
    ensure_usb_backend,
)

TICKS_PER_S = 1024


def cadence_frames(rpm: float, count: int, start_revs: int = 0, start_ticks: int = 0):
    """Crank counter samples for a steady cadence, one notification per rev."""
    seconds_per_rev = 60.0 / rpm
    frames = []
    for i in range(1, count + 1):
        frames.append(
            (
                (start_revs + i) % (1 << 16),
                round(start_ticks + i * seconds_per_rev * TICKS_PER_S) % (1 << 16),
                i * seconds_per_rev,
            )
        )
    return frames


class TestCrankCadenceTracker:
    def test_first_sample_cannot_yield_a_cadence(self):
        tracker = CrankCadenceTracker()
        assert tracker.update(10, 1024, 0.0) is None

    def test_steady_cadence_is_recovered(self):
        tracker = CrankCadenceTracker()
        results = [tracker.update(*frame) for frame in cadence_frames(90.0, 6)]
        assert results[0] is None
        for value in results[1:]:
            assert value == pytest.approx(90.0, abs=0.5)

    @pytest.mark.parametrize("rpm", [60.0, 70.0, 90.0, 110.0])
    def test_cadence_is_recovered_across_the_protocol_range(self, rpm):
        tracker = CrankCadenceTracker()
        results = [tracker.update(*frame) for frame in cadence_frames(rpm, 5)]
        assert results[-1] == pytest.approx(rpm, abs=0.5)

    def test_event_time_rollover_does_not_produce_a_garbage_spike(self):
        """The 1/1024 s event clock wraps every 64 seconds, mid-session."""
        tracker = CrankCadenceTracker()
        tracker.update(100, 65000, 0.0)
        # Next revolution lands after the 16-bit tick counter wraps.
        cadence = tracker.update(101, (65000 + 683) % (1 << 16), 0.667)
        assert cadence == pytest.approx(90.0, abs=1.0)

    def test_revolution_counter_rollover_is_handled(self):
        tracker = CrankCadenceTracker()
        tracker.update(65535, 1000, 0.0)
        cadence = tracker.update(0, 1000 + 683, 0.667)
        assert cadence == pytest.approx(90.0, abs=1.0)

    def test_repeated_notification_holds_the_last_cadence(self):
        """Notifications arrive faster than crank events at low cadence."""
        tracker = CrankCadenceTracker()
        for frame in cadence_frames(70.0, 3):
            tracker.update(*frame)
        held = tracker.update(3, round(3 * 60 / 70 * TICKS_PER_S), 2.6)
        assert held == pytest.approx(70.0, abs=0.5)

    def test_a_long_silence_reads_as_coasting(self):
        tracker = CrankCadenceTracker(coast_timeout_s=3.0)
        for frame in cadence_frames(90.0, 3):
            tracker.update(*frame)
        revs, ticks, t = 3, round(3 * 60 / 90 * TICKS_PER_S), 2.0
        assert tracker.update(revs, ticks, t + 1.0) == pytest.approx(90.0, abs=0.5)
        assert tracker.update(revs, ticks, t + 10.0) == 0.0

    def test_missing_counters_yield_no_cadence(self):
        tracker = CrankCadenceTracker()
        assert tracker.update(None, 1024, 0.0) is None
        assert tracker.update(10, None, 0.0) is None

    def test_implausible_cadence_is_rejected_rather_than_reported(self):
        tracker = CrankCadenceTracker()
        for frame in cadence_frames(90.0, 3):
            tracker.update(*frame)
        # A single tick between two revolutions implies tens of thousands of rpm.
        assert tracker.update(5, round(3 * 60 / 90 * TICKS_PER_S) + 1, 2.5) == pytest.approx(
            90.0, abs=0.5
        )


class TestFieldExtraction:
    """The field names each library actually uses, pinned against real payloads.

    Verified against pycycling 0.4.1 and openant 1.3.4: FTMS indoor bike data
    exposes `instant_power`/`instant_cadence`, the BLE power measurement exposes
    `instantaneous_power` and no cadence at all, and ANT+ exposes
    `instantaneous_power`/`cadence`.
    """

    def test_ftms_indoor_bike_data_fields_are_covered(self):
        class IndoorBikeData:
            instant_speed = 30.0
            instant_cadence = 85.0
            instant_power = 250
            average_power = None

        data = IndoorBikeData()
        assert _first_attr(data, FtmsTrainer.POWER_FIELDS) == 250
        assert _first_attr(data, FtmsTrainer.CADENCE_FIELDS) == 85.0

    def test_ant_plus_power_data_fields_are_covered(self):
        class PowerData:
            instantaneous_power = 245
            cadence = 88
            average_power = 240

        data = PowerData()
        assert _first_attr(data, AntPlusPedals.POWER_FIELDS) == 245
        assert _first_attr(data, AntPlusPedals.CADENCE_FIELDS) == 88

    def test_ble_power_measurement_power_field_is_covered(self):
        class CyclingPowerMeasurement:
            instantaneous_power = 245
            cumulative_crank_revs = 1234
            last_crank_event_time = 20000

        data = CyclingPowerMeasurement()
        assert _first_attr(data, BlePedals.POWER_FIELDS) == 245

    def test_first_attr_skips_absent_and_none_fields(self):
        class Partial:
            power = None
            instant_power = 200

        assert _first_attr(Partial(), ("missing", "power", "instant_power")) == 200
        assert _first_attr(Partial(), ("missing",)) is None


class TestAntErrorHints:
    """The two USB failures that dominate on Windows must explain themselves.

    pyusb raises "No backend available" when libusb is absent, and openant
    raises DriverNotFound with an *empty* message when libusb works but no stick
    can be claimed. Passed through verbatim, neither tells you what to do.
    """

    def test_missing_libusb_names_the_fix(self):
        class NoBackendError(Exception):
            pass

        hint = ant_error_hint(NoBackendError("No backend available"))
        assert "libusb-package" in hint
        assert "Zadig" in hint
        assert "BLE" in hint  # the workaround available right now

    def test_missing_libusb_is_detected_by_message_too(self):
        hint = ant_error_hint(ValueError("No backend available"))
        assert "libusb-package" in hint

    def test_empty_driver_not_found_still_explains_itself(self):
        class DriverNotFound(Exception):
            pass

        hint = ant_error_hint(DriverNotFound(""))
        assert hint.strip()
        assert "Zadig" in hint
        assert "plugged in" in hint

    def test_busy_device_points_at_the_holder(self):
        class USBError(Exception):
            pass

        hint = ant_error_hint(USBError("[Errno 13] Access denied (insufficient permissions)"))
        assert "holding it" in hint

    def test_unknown_failure_keeps_the_original_text(self):
        assert "something odd" in ant_error_hint(RuntimeError("something odd"))

    def test_unknown_failure_without_a_message_still_names_the_type(self):
        class WeirdError(Exception):
            pass

        assert "WeirdError" in ant_error_hint(WeirdError(""))


class TestUsbBackend:
    def test_reports_how_the_backend_was_obtained(self):
        pytest.importorskip("usb.backend.libusb1")
        assert ensure_usb_backend() in {"system", "bundled", "unavailable"}

    def test_a_working_system_backend_is_left_alone(self, monkeypatch):
        libusb1 = pytest.importorskip("usb.backend.libusb1")
        monkeypatch.setattr(libusb1, "get_backend", lambda find_library=None: object())
        assert ensure_usb_backend() == "system"

    def test_bundled_libusb_is_primed_when_the_system_has_none(self, monkeypatch):
        """This is the Windows path: prime pyusb so openant's own find() works."""
        libusb1 = pytest.importorskip("usb.backend.libusb1")
        sentinel = object()
        passed = {}

        def fake_get_backend(find_library=None):
            if find_library is None:
                return None
            passed["path"] = find_library("usb-1.0")
            return sentinel

        monkeypatch.setattr(libusb1, "get_backend", fake_get_backend)
        monkeypatch.setitem(
            sys.modules,
            "libusb_package",
            types.SimpleNamespace(get_library_path=lambda: "/somewhere/libusb-1.0.dll"),
        )
        assert ensure_usb_backend() == "bundled"
        assert passed["path"] == "/somewhere/libusb-1.0.dll"

    def test_absent_libusb_package_is_reported_not_raised(self, monkeypatch):
        libusb1 = pytest.importorskip("usb.backend.libusb1")
        monkeypatch.setattr(libusb1, "get_backend", lambda find_library=None: None)
        monkeypatch.setitem(sys.modules, "libusb_package", None)
        assert ensure_usb_backend() == "unavailable"


class StubClock:
    """A clock whose time is set by the test rather than elapsing."""

    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds


class TestRealPayloadParsing:
    """End-to-end from protocol bytes to a Sample, using the real parsers.

    This is as close to the hardware as it gets without a radio, and it is what
    catches a library renaming a field out from under the adapters.
    """

    def test_ftms_bytes_become_a_trainer_sample(self):
        parsers = pytest.importorskip("pycycling.ftms_parsers.indoor_bike_data")
        import struct

        clock = StubClock()
        trainer = FtmsTrainer("AA:BB:CC:DD:EE:FF", clock)
        samples = []
        trainer.add_handler(samples.append)

        flags = (1 << 2) | (1 << 6)  # instantaneous cadence and power present
        payload = struct.pack("<HHHh", flags, 3000, 170, 250)  # 85 rpm, 250 W
        trainer._on_indoor_bike_data(parsers.parse_indoor_bike_data(payload))

        assert len(samples) == 1
        assert samples[0].watts == 250.0
        assert samples[0].cadence == 85.0

    def test_ble_power_bytes_become_pedal_samples_with_derived_cadence(self):
        cps = pytest.importorskip("pycycling.cycling_power_service")
        import struct

        clock = StubClock()
        pedals = BlePedals("AA:BB:CC:DD:EE:FF", clock)
        samples = []
        pedals.add_handler(samples.append)

        flags = 1 << 5  # crank revolution data present
        seconds_per_rev = 60.0 / 90.0
        for rev in range(1, 5):
            clock.t = rev * seconds_per_rev
            payload = struct.pack(
                "<HhHH", flags, 245, rev, round(rev * seconds_per_rev * TICKS_PER_S)
            )
            pedals._on_measurement(cps._parse_cycling_power_measurement(payload))

        assert [s.watts for s in samples] == [245.0] * 4
        assert samples[0].cadence is None  # no previous crank event to compare
        for sample in samples[1:]:
            assert sample.cadence == pytest.approx(90.0, abs=0.5)
