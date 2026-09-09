"""Tests for the hardware adapters' pure logic.

The radio paths cannot be exercised without a trainer, but the parts that
actually interpret device data can be, and those are where the bugs hide.
`devices` defers every optional import, so this module loads without bleak or
pycycling present.
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

from power_meter_audit.live.devices import (
    CPS_MEASUREMENT_UUID,
    FTMS_CONTROL_POINT_UUID,
    FTMS_INDOOR_BIKE_DATA_UUID,
    BlePedals,
    CrankCadenceTracker,
    FtmsTrainer,
    _first_attr,
    describe_characteristics,
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

    Verified against pycycling 0.4.1: FTMS indoor bike data exposes
    `instant_power`/`instant_cadence`, and the BLE power measurement exposes
    `instantaneous_power` and no cadence at all.
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


class StubClock:
    """A clock whose time is set by the test rather than elapsing."""

    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds


async def _noop(*args, **kwargs):
    return None


class FakeServices:
    """Stands in for bleak's discovered characteristic table."""

    def __init__(self, uuids):
        self._uuids = {u.lower() for u in uuids}
        self.characteristics = {
            i: types.SimpleNamespace(uuid=u) for i, u in enumerate(sorted(self._uuids))
        }

    def get_characteristic(self, specifier):
        return types.SimpleNamespace(uuid=specifier) if str(specifier).lower() in self._uuids else None


class FakeFtms:
    """Records the handshake order, since that is what the ordering bug was."""

    def __init__(self, serves):
        self.calls = []
        self._serves = serves

    def set_indoor_bike_data_handler(self, handler):
        self.calls.append("set_data_handler")

    async def enable_indoor_bike_data_notify(self):
        self.calls.append("enable_data_notify")
        if FTMS_INDOOR_BIKE_DATA_UUID not in self._serves:
            raise RuntimeError(f"Characteristic {FTMS_INDOOR_BIKE_DATA_UUID} was not found!")

    async def start_or_resume(self):
        self.calls.append("start_or_resume")

    async def enable_control_point_indicate(self):
        self.calls.append("enable_control_indicate")

    async def request_control(self):
        self.calls.append("request_control")

    async def reset(self):
        self.calls.append("reset")


class TestTrainerReadingSource:
    """A trainer must end up controllable regardless of which power service it
    serves. Wahoo exposes both FTMS Indoor Bike Data and the Cycling Power
    Service, and a firmware missing the former used to abort the connect before
    control was ever requested, leaving ERG silently disengaged all session.
    """

    def _trainer(self, serves):
        trainer = FtmsTrainer("AA:BB:CC:DD:EE:FF", StubClock())
        trainer._client = types.SimpleNamespace(services=FakeServices(serves))
        trainer._ftms = FakeFtms(serves)
        return trainer

    def test_indoor_bike_data_is_preferred_when_served(self):
        trainer = self._trainer({FTMS_INDOOR_BIKE_DATA_UUID, CPS_MEASUREMENT_UUID})
        asyncio.run(trainer._subscribe_to_readings())
        assert trainer.data_source == "FTMS indoor bike data"

    def test_falls_back_to_cycling_power_when_indoor_bike_data_is_absent(self, monkeypatch):
        trainer = self._trainer({CPS_MEASUREMENT_UUID})
        created = {}

        class FakeCps:
            def __init__(self, client):
                created["client"] = client

            def set_cycling_power_measurement_handler(self, handler):
                created["handler"] = handler

            async def enable_cycling_power_measurement_notifications(self):
                created["subscribed"] = True

        monkeypatch.setitem(
            sys.modules,
            "pycycling.cycling_power_service",
            types.SimpleNamespace(CyclingPowerService=FakeCps),
        )
        asyncio.run(trainer._subscribe_to_readings())

        assert trainer.data_source == "cycling power service"
        assert created["subscribed"]
        # The failing subscribe must never be attempted, so it cannot raise.
        assert "enable_data_notify" not in trainer._ftms.calls

    def test_no_usable_service_is_reported_with_what_was_found(self):
        trainer = self._trainer({FTMS_CONTROL_POINT_UUID})
        trainer.characteristics = describe_characteristics(trainer._client)
        with pytest.raises(RuntimeError) as excinfo:
            asyncio.run(trainer._subscribe_to_readings())
        assert "0x2AD2" in str(excinfo.value)
        assert "FTMS control point" in str(excinfo.value)

    def test_cycling_power_readings_carry_derived_cadence(self, monkeypatch):
        trainer = self._trainer({CPS_MEASUREMENT_UUID})
        samples = []
        trainer.add_handler(samples.append)

        class Measurement:
            instantaneous_power = 210
            cumulative_crank_revs = 0
            last_crank_event_time = 0

        first = Measurement()
        trainer._on_power_measurement(first)
        second = Measurement()
        second.cumulative_crank_revs = 1
        second.last_crank_event_time = round(TICKS_PER_S * 60 / 90)
        trainer._clock.t = 60 / 90
        trainer._on_power_measurement(second)

        assert [s.watts for s in samples] == [210.0, 210.0]
        assert samples[1].cadence == pytest.approx(90.0, abs=0.5)

    def test_control_is_established_before_any_data_subscription(self, monkeypatch):
        """The ordering is the whole bug: a trainer that does not serve Indoor
        Bike Data raised on subscribe, aborting connect before control was
        requested, so ERG was never enabled and every target write was ignored.
        """
        serves = {FTMS_CONTROL_POINT_UUID, FTMS_INDOOR_BIKE_DATA_UUID}
        ftms = FakeFtms(serves)
        client = types.SimpleNamespace(
            services=FakeServices(serves), connect=_noop, disconnect=_noop
        )

        monkeypatch.setitem(
            sys.modules, "bleak", types.SimpleNamespace(BleakClient=lambda address: client)
        )
        monkeypatch.setitem(
            sys.modules,
            "pycycling.fitness_machine_service",
            types.SimpleNamespace(FitnessMachineService=lambda c: ftms),
        )

        trainer = FtmsTrainer("AA:BB:CC:DD:EE:FF", StubClock())
        asyncio.run(trainer.connect())

        assert trainer.connected
        assert ftms.calls.index("request_control") < ftms.calls.index("enable_data_notify")
        assert ftms.calls.index("enable_control_indicate") < ftms.calls.index("request_control")
        # Reset can revoke the control it was just granted, so it is retaken.
        assert ftms.calls.count("request_control") == 2
        assert ftms.calls.index("reset") < ftms.calls.index("enable_data_notify")
        # Start-or-Resume is what actually engages ERG; without it, target
        # writes are accepted and the trainer freewheels.
        assert "start_or_resume" in ftms.calls
        assert ftms.calls.index("start_or_resume") < ftms.calls.index("enable_data_notify")

    def test_a_second_connect_does_not_rebuild_the_ble_client(self, monkeypatch):
        """Start must not open a second exclusive BLE link on an already-live trainer."""
        serves = {FTMS_CONTROL_POINT_UUID, FTMS_INDOOR_BIKE_DATA_UUID}
        clients: list[object] = []

        def make_client(address):
            client = types.SimpleNamespace(
                services=FakeServices(serves), connect=_noop, disconnect=_noop
            )
            clients.append(client)
            return client

        monkeypatch.setitem(sys.modules, "bleak", types.SimpleNamespace(BleakClient=make_client))
        monkeypatch.setitem(
            sys.modules,
            "pycycling.fitness_machine_service",
            types.SimpleNamespace(FitnessMachineService=lambda c: FakeFtms(serves)),
        )

        trainer = FtmsTrainer("AA:BB:CC:DD:EE:FF", StubClock())
        asyncio.run(trainer.connect())
        first = trainer._client
        asyncio.run(trainer.connect())

        assert trainer._client is first
        assert len(clients) == 1

    def test_serves_matches_uuid_objects_and_dashed_strings(self):
        import uuid

        trainer = FtmsTrainer("AA:BB:CC:DD:EE:FF", StubClock())
        trainer._client = types.SimpleNamespace(
            services=types.SimpleNamespace(
                characteristics={
                    1: types.SimpleNamespace(uuid=uuid.UUID(FTMS_INDOOR_BIKE_DATA_UUID)),
                    2: types.SimpleNamespace(uuid=CPS_MEASUREMENT_UUID.upper()),
                }
            )
        )
        assert trainer._serves(FTMS_INDOOR_BIKE_DATA_UUID)
        assert trainer._serves(CPS_MEASUREMENT_UUID)
        assert not trainer._serves(FTMS_CONTROL_POINT_UUID)

    def test_characteristics_are_named_for_diagnosis(self):
        described = describe_characteristics(
            types.SimpleNamespace(
                services=FakeServices({FTMS_CONTROL_POINT_UUID, CPS_MEASUREMENT_UUID})
            )
        )
        assert any("FTMS control point" in d for d in described)
        assert any("cycling power measurement" in d for d in described)


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
