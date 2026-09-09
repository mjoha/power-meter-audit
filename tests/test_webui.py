"""Tests for the browser UI server, driven against the simulated rig.

Time is accelerated so a whole protocol completes inside a test.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

aiohttp = pytest.importorskip("aiohttp")
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from power_meter_audit.live.protocol import quick_protocol, standard_protocol  # noqa: E402
from power_meter_audit.live.simulator import SimulatedPedals  # noqa: E402
from power_meter_audit.live.webui.server import (  # noqa: E402
    STATIC_DIR,
    CompareServer,
    create_app,
    protocol_from_payload,
    protocol_to_payload,
)

FAST_PROTOCOL = {
    "name": "test",
    "warmup_s": 0,
    "cadence_tolerance_rpm": 5,
    "steps": [
        {"watts": 150, "label": "150W", "cadences": [70, 90], "seconds_per_cadence": 60},
        {"watts": 300, "label": "300W", "cadences": [70, 90], "seconds_per_cadence": 60},
    ],
}


@contextlib.asynccontextmanager
async def client():
    server = CompareServer()
    app = create_app(server)
    test_client = TestClient(TestServer(app))
    await test_client.start_server()
    try:
        yield test_client, server
    finally:
        await test_client.close()


async def _wait_for_phase(test_client, phase: str, timeout: float = 30.0) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        state = await (await test_client.get("/api/state")).json()
        if state["phase"] == phase:
            return state
        await asyncio.sleep(0.05)
    raise AssertionError(f"never reached phase {phase!r}")


def test_static_assets_are_packaged():
    for name in ("index.html", "app.js", "style.css"):
        assert (STATIC_DIR / name).is_file()


def test_protocol_payload_round_trips():
    original = standard_protocol()
    restored = protocol_from_payload(protocol_to_payload(original))

    assert restored.total_duration_s == original.total_duration_s
    assert restored.measured_segment_count == original.measured_segment_count
    assert [s.target_watts for s in restored.steps] == [s.target_watts for s in original.steps]


def test_protocol_payload_accepts_a_preset_name():
    assert protocol_from_payload({"preset": "quick"}).name == quick_protocol().name


def test_protocol_payload_rejects_an_empty_ladder():
    with pytest.raises(ValueError):
        protocol_from_payload({"steps": []})


def test_index_and_initial_state_are_served():
    async def scenario():
        async with client() as (test_client, _):
            page = await test_client.get("/")
            assert page.status == 200
            assert "Power meter comparison" in await page.text()

            state = await (await test_client.get("/api/state")).json()
            assert state["phase"] == "idle"
            assert state["protocol"]["cell_count"] == standard_protocol().measured_segment_count
            assert state["sources"]["trainer"]["watts"] is None

    asyncio.run(scenario())


def test_connecting_the_simulator_starts_streaming():
    async def scenario():
        async with client() as (test_client, server):
            response = await test_client.post(
                "/api/connect", json={"mode": "simulate", "speed": 20}
            )
            assert response.status == 200
            assert (await response.json())["phase"] == "connected"

            # The rig only produces samples once time is advancing.
            await asyncio.sleep(0.5)
            state = await (await test_client.get("/api/state")).json()
            assert state["sources"]["trainer"]["alive"]
            assert state["sources"]["pedals"]["alive"]
            assert state["sources"]["pedals"]["hz"] > state["sources"]["trainer"]["hz"]

            await test_client.post("/api/disconnect")
            assert (await (await test_client.get("/api/state")).json())["phase"] == "idle"

    asyncio.run(scenario())


def test_hardware_mode_without_an_address_is_rejected():
    async def scenario():
        async with client() as (test_client, _):
            response = await test_client.post("/api/connect", json={"mode": "hardware"})
            assert response.status == 400
            assert "trainer address" in (await response.json())["error"]

    asyncio.run(scenario())


def test_one_radio_failing_leaves_the_other_usable_and_named():
    """Reproduces a real hardware session: the trainer connected over BLE and
    streamed data, but the ANT+ pedals failed for want of libusb. The rig must
    not claim to be disconnected while a source is live, and it must say which
    source failed rather than showing a bare error.
    """

    async def scenario():
        async with client() as (test_client, server):

            async def failing_connect(self):
                raise RuntimeError("ANT+ needs libusb, which Windows does not ship")

            original = SimulatedPedals.connect
            SimulatedPedals.connect = failing_connect
            try:
                await test_client.post("/api/connect", json={"mode": "simulate", "speed": 20})
            finally:
                SimulatedPedals.connect = original

            await asyncio.sleep(0.4)
            state = await (await test_client.get("/api/state")).json()

            assert state["phase"] == "partial"
            assert state["sources"]["trainer"]["alive"]  # trainer really is working
            assert state["sources"]["trainer"]["error"] is None
            assert "libusb" in state["sources"]["pedals"]["error"]
            assert "libusb" in state["error"]

            # Comparing needs both, and the refusal has to say so.
            response = await test_client.post("/api/start")
            assert response.status == 400
            assert "both sources" in (await response.json())["error"]

    asyncio.run(scenario())


def test_starting_before_connecting_is_rejected():
    async def scenario():
        async with client() as (test_client, _):
            response = await test_client.post("/api/start")
            assert response.status == 400
            assert "connect" in (await response.json())["error"]

    asyncio.run(scenario())


def test_full_run_produces_a_report_and_downloads():
    async def scenario():
        async with client() as (test_client, _):
            await test_client.post("/api/protocol", json=FAST_PROTOCOL)
            await test_client.post(
                "/api/connect",
                json={"mode": "simulate", "speed": 200, "pedal_torque_gain": 0.003},
            )
            await test_client.post("/api/start")

            state = await _wait_for_phase(test_client, "finished")
            report = state["report"]
            assert report is not None
            assert len(report["cells"]) == 4
            assert report["consistency"] == "red"  # injected torque-dependent fault
            assert report["ratio_by_cadence"]["70"] > report["ratio_by_cadence"]["90"]
            assert state["progress"] == pytest.approx(1.0, abs=0.05)
            assert "Dual power source comparison" in state["report_text"]

            session = await test_client.get("/api/session.json")
            assert session.status == 200
            assert len((await session.json())["samples"]) > 50

            csv = await test_client.get("/api/session.csv")
            assert csv.status == 200
            assert "t_s,source,watts" in await csv.text()

    asyncio.run(scenario())


def test_run_still_records_after_a_long_connection_preview():
    """A preview advances simulated time a long way before the session starts.

    The run then restarts the session clock at zero, and the rig has to be
    rewound with it — otherwise its next-sample times stay parked in the future
    and the whole session records nothing.
    """

    async def scenario():
        async with client() as (test_client, server):
            await test_client.post("/api/protocol", json=FAST_PROTOCOL)
            await test_client.post("/api/connect", json={"mode": "simulate", "speed": 300})

            # Idle on the verify screen long enough to push simulated time well
            # past the end of the protocol that is about to run.
            await asyncio.sleep(2.0)
            preview = await (await test_client.get("/api/state")).json()
            assert preview["sources"]["trainer"]["alive"]

            await test_client.post("/api/start")
            state = await _wait_for_phase(test_client, "finished")

            report = state["report"]
            assert all(cell["usable"] for cell in report["cells"]), [
                (cell["label"], cell["reason"]) for cell in report["cells"]
            ]
            assert all(cell["trainer_n"] > 0 and cell["pedal_n"] > 0 for cell in report["cells"])
            assert report["consistency"] != "unknown"
            assert report["mean_ratio"] is not None

    asyncio.run(scenario())


def test_accelerated_time_keeps_the_sample_rate_in_session_time():
    """Sample density must come from the session clock, not the tick rate."""

    async def scenario():
        async with client() as (test_client, server):
            await test_client.post("/api/protocol", json=FAST_PROTOCOL)
            await test_client.post("/api/connect", json={"mode": "simulate", "speed": 300})
            await test_client.post("/api/start")
            await _wait_for_phase(test_client, "finished")

            log = server.log
            assert log is not None
            # 240 s of session time at 1 Hz and 4 Hz, allowing for edges.
            trainer = [s for s in log.samples if s.source == "trainer"]
            pedals = [s for s in log.samples if s.source == "pedals"]
            assert 200 < len(trainer) < 280
            assert 3.5 < len(pedals) / len(trainer) < 4.5

    asyncio.run(scenario())


def test_live_status_reports_cadence_guidance_during_a_run():
    async def scenario():
        async with client() as (test_client, _):
            await test_client.post("/api/protocol", json=FAST_PROTOCOL)
            await test_client.post("/api/connect", json={"mode": "simulate", "speed": 20})
            await test_client.post("/api/start")

            seen_targets: set[int] = set()
            deadline = asyncio.get_running_loop().time() + 20.0
            while asyncio.get_running_loop().time() < deadline:
                state = await (await test_client.get("/api/state")).json()
                segment = state.get("segment")
                if segment and segment["target_rpm"] is not None:
                    seen_targets.add(segment["target_rpm"])
                    assert "cadence_in_zone" in segment
                    assert 0 <= state["progress"] <= 1
                if state["phase"] == "finished" or seen_targets >= {70, 90}:
                    break
                await asyncio.sleep(0.05)

            assert seen_targets == {70, 90}
            assert len(state["trace"]) > 5
            await test_client.post("/api/stop")

    asyncio.run(scenario())


def test_downloads_404_before_a_session_exists():
    async def scenario():
        async with client() as (test_client, _):
            assert (await test_client.get("/api/session.json")).status == 404
            assert (await test_client.get("/api/session.csv")).status == 404

    asyncio.run(scenario())
