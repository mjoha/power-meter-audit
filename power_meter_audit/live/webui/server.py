"""Local browser UI for running and reviewing a dual power source comparison.

Runs an aiohttp server on localhost. The browser is only a view: all device
handling, protocol execution and analysis happen in this process, and state is
pushed to the page over a websocket.
"""

from __future__ import annotations

import asyncio
import json
import time
import webbrowser
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

from power_meter_audit.live.analysis import (
    Thresholds,
    analyse_session,
    comparison_to_dict,
    format_comparison_report,
)
from power_meter_audit.live.harness import SimulationDriver, build_runner
from power_meter_audit.live.protocol import PRESETS, CadenceSegment, Protocol, Step
from power_meter_audit.live.runner import (
    ControlWarning,
    LiveStatus,
    SegmentStarted,
    SessionRunner,
)
from power_meter_audit.live.session import SessionLog
from power_meter_audit.live.simulator import MeterModel, SimulatedRig
from power_meter_audit.live.sources import PEDALS, TRAINER, PowerSource, ScaledClock, Sample

STATIC_DIR = Path(__file__).parent / "static"
PUSH_INTERVAL_S = 0.2
RATE_WINDOW_S = 5.0


@dataclass
class SourceMonitor:
    """Tracks the most recent value and the arrival rate of one source."""

    label: str = ""
    last: Sample | None = None
    error: str | None = None
    detail: str | None = None
    arrivals: deque = field(default_factory=lambda: deque(maxlen=200))

    def record(self, sample: Sample) -> None:
        self.last = sample
        self.arrivals.append(time.monotonic())

    @property
    def hz(self) -> float:
        now = time.monotonic()
        recent = [t for t in self.arrivals if now - t <= RATE_WINDOW_S]
        if len(recent) < 2:
            return 0.0
        return (len(recent) - 1) / max(recent[-1] - recent[0], 1e-6)

    @property
    def alive(self) -> bool:
        return bool(self.arrivals) and (time.monotonic() - self.arrivals[-1]) < 3.0

    def snapshot(self) -> dict:
        # A source that failed to connect must not show numbers, whatever is
        # still arriving from it. Reporting watts next to the failure reads as
        # "working anyway" and is the opposite of what happened.
        if self.error is not None:
            return {
                "label": self.label,
                "watts": None,
                "cadence": None,
                "hz": 0.0,
                "alive": False,
                "error": self.error,
                "detail": self.detail,
            }
        return {
            "label": self.label,
            "watts": self.last.watts if self.last else None,
            "cadence": self.last.cadence if self.last else None,
            "hz": round(self.hz, 2),
            "alive": self.alive,
            "error": None,
            "detail": self.detail,
        }


def protocol_to_payload(protocol: Protocol) -> dict:
    steps = []
    for step in protocol.steps:
        steps.append(
            {
                "watts": step.target_watts,
                "label": step.label,
                "cadences": [seg.target_rpm for seg in step.segments],
                "seconds_per_cadence": step.segments[0].duration_s if step.segments else 0,
            }
        )
    return {
        "name": protocol.name,
        "warmup_s": protocol.warmup_s,
        "warmup_watts": protocol.warmup_watts,
        "power_settle_s": protocol.power_settle_s,
        "cadence_settle_s": protocol.cadence_settle_s,
        "cadence_tolerance_rpm": protocol.cadence_tolerance_rpm,
        "steps": steps,
        "total_duration_s": protocol.total_duration_s,
        "cell_count": protocol.measured_segment_count,
        "notes": protocol.notes,
    }


def protocol_from_payload(payload: dict) -> Protocol:
    if payload.get("preset"):
        return PRESETS[payload["preset"]]()

    steps: list[Step] = []
    for row in payload.get("steps", []):
        watts = int(row["watts"])
        seconds = int(row.get("seconds_per_cadence", 120))
        cadences = [int(c) for c in row.get("cadences", [70, 90])]
        steps.append(
            Step(
                target_watts=watts,
                segments=tuple(CadenceSegment(rpm, seconds) for rpm in cadences),
                label=row.get("label") or f"{watts}W",
            )
        )
    if not steps:
        raise ValueError("protocol needs at least one step")

    return Protocol(
        name=payload.get("name") or "custom",
        steps=steps,
        warmup_s=int(payload.get("warmup_s", 600)),
        warmup_watts=int(payload.get("warmup_watts", 120)),
        cadence_tolerance_rpm=float(payload.get("cadence_tolerance_rpm", 5.0)),
        notes=payload.get("notes", []),
    )


class CompareServer:
    def __init__(self, thresholds: Thresholds | None = None) -> None:
        self.thresholds = thresholds or Thresholds()
        self.phase = "idle"
        self.mode = "simulate"
        self.protocol: Protocol = PRESETS["standard"]()
        self.monitors: dict[str, SourceMonitor] = {
            TRAINER: SourceMonitor("Trainer"),
            PEDALS: SourceMonitor("Pedals"),
        }
        self.status: LiveStatus | None = None
        self.warnings: list[str] = []
        self.log: SessionLog | None = None
        self.report: dict | None = None
        self.report_text: str = ""
        self.error: str | None = None

        self._clock: ScaledClock | None = None
        self._trainer: PowerSource | None = None
        self._pedals: PowerSource | None = None
        self._driver: SimulationDriver | None = None
        self._runner: SessionRunner | None = None
        self._run_task: asyncio.Task | None = None
        self._sockets: set[web.WebSocketResponse] = set()
        self._push_task: asyncio.Task | None = None
        self._trace: list[dict] = []
        self._last_trace_t = -1.0
        self._rig: SimulatedRig | None = None

    # ---------- device lifecycle ----------

    async def connect(self, payload: dict) -> None:
        await self.disconnect()
        self.error = None
        self.mode = payload.get("mode", "simulate")
        self._clock = ScaledClock(float(payload.get("speed", 1.0)))

        if self.mode == "simulate":
            rig = SimulatedRig(
                pedal_model=MeterModel(
                    left_fraction=float(payload.get("left_fraction", 0.5)),
                    scale=float(payload.get("pedal_scale", 1.0)),
                    torque_gain=float(payload.get("pedal_torque_gain", 0.0)),
                    noise_w=3.0,
                )
            )
            self._trainer, self._pedals = rig.trainer, rig.pedals
            self._driver = SimulationDriver(rig, self._clock)
            self._rig = rig
        else:
            from power_meter_audit.live.devices import AntPlusPedals, BlePedals, FtmsTrainer

            address = payload.get("trainer_address")
            if not address:
                raise ValueError("a trainer address is required for a hardware run")
            self._trainer = FtmsTrainer(address, self._clock)
            self._pedals = (
                BlePedals(payload["pedals_ble"], self._clock)
                if payload.get("pedals_ble")
                else AntPlusPedals(self._clock, device_id=int(payload.get("ant_device_id", 0)))
            )
            self._rig = None
            self._driver = None

        for name, source in ((TRAINER, self._trainer), (PEDALS, self._pedals)):
            monitor = self.monitors[name]
            monitor.last = None
            monitor.arrivals.clear()
            monitor.label = source.label
            monitor.error = None
            source.add_handler(monitor.record)

        # Connect the sources independently. One radio failing says nothing
        # about the other, and a half-connected rig that reports itself as
        # disconnected is worse than one that names the source that failed.
        for name, source in ((TRAINER, self._trainer), (PEDALS, self._pedals)):
            try:
                await source.connect()
            except Exception as exc:  # noqa: BLE001 - reported per source
                self.monitors[name].error = str(exc) or type(exc).__name__
            # Which of a trainer's overlapping power services is actually in use
            # is worth stating rather than leaving to be inferred.
            self.monitors[name].detail = getattr(source, "data_source", None)

        if self._driver is not None:
            await self._driver.start()

        live = [name for name in (TRAINER, PEDALS) if self.monitors[name].error is None]
        self.phase = "connected" if len(live) == 2 else "partial" if live else "idle"
        failures = [
            f"{self.monitors[name].label}: {self.monitors[name].error}"
            for name in (TRAINER, PEDALS)
            if self.monitors[name].error
        ]
        self.error = "  ".join(failures) or None
        await self._ensure_push_task()

    async def disconnect(self) -> None:
        await self.stop()
        if self._driver is not None:
            await self._driver.stop()
            self._driver = None
        for source in (self._trainer, self._pedals):
            if source is not None:
                try:
                    await source.disconnect()
                except Exception as exc:  # noqa: BLE001 - teardown is best effort
                    self.warnings.append(f"disconnect failed: {exc}")
        self._trainer = self._pedals = None
        if self.phase in {"connected", "partial", "running"}:
            self.phase = "idle"

    # ---------- session lifecycle ----------

    async def start(self) -> None:
        if self.phase == "partial":
            offline = [m.label for m in self.monitors.values() if m.error]
            raise ValueError(
                f"comparing needs both sources, and {', '.join(offline)} did not connect"
            )
        if self.phase not in {"connected", "finished"}:
            raise ValueError("connect the devices first")
        if self._trainer is None or self._pedals is None or self._clock is None:
            raise ValueError("devices are not connected")

        self.warnings.clear()
        self._trace.clear()
        self._last_trace_t = -1.0
        self.report = None
        self.report_text = ""
        self.error = None

        # Restart the clock so session time begins at zero for this run. The
        # simulated rig has to be rewound with it, or its next-sample times stay
        # parked wherever the connection preview left them and it emits nothing.
        if self._driver is not None:
            await self._driver.stop()
        self._clock = ScaledClock(self._clock.speed)
        for source in (self._trainer, self._pedals):
            source.set_clock(self._clock)
        self._last_trace_t = -1.0
        if self._rig is not None:
            self._rig.reset()
            self._driver = SimulationDriver(self._rig, self._clock)
            await self._driver.start()

        self._runner = build_runner(
            self.protocol,
            self._trainer,
            self._pedals,
            clock=self._clock,
            on_event=self._on_event,
            extra_segment_hook=self._rig.set_target_cadence if self._rig else None,
        )
        self.phase = "running"
        self._run_task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            self.log = await self._runner.run()
            report = analyse_session(self.log, self.thresholds)
            self.report = comparison_to_dict(report)
            self.report_text = format_comparison_report(report)
            self.phase = "finished"
        except asyncio.CancelledError:
            self.phase = "connected"
        except Exception as exc:  # noqa: BLE001 - surfaced to the browser
            self.error = str(exc)
            self.phase = "connected"
        finally:
            await self._broadcast()

    async def stop(self) -> None:
        if self._runner is not None:
            self._runner.stop()
        if self._run_task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._run_task), timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._run_task = None

    def _on_event(self, event: Any) -> None:
        if isinstance(event, LiveStatus):
            self.status = event
            self._append_trace(event)
        elif isinstance(event, ControlWarning):
            self.warnings.append(event.message)
        elif isinstance(event, SegmentStarted):
            self.status = None

    def _append_trace(self, status: LiveStatus) -> None:
        # One point per second of session time keeps the chart payload bounded
        # regardless of how fast simulated time is running.
        if status.t - self._last_trace_t < 1.0:
            return
        self._last_trace_t = status.t
        trainer = self.monitors[TRAINER].last
        pedals = self.monitors[PEDALS].last
        self._trace.append(
            {
                "t": round(status.t, 1),
                "trainer": trainer.watts if trainer else None,
                "pedals": pedals.watts if pedals else None,
                "cadence": trainer.cadence if trainer else None,
                "target_watts": status.segment.target_watts,
                "target_rpm": status.segment.target_rpm,
            }
        )
        # Keep the live chart bounded; the full record lives in the session log.
        if len(self._trace) > 3000:
            del self._trace[: len(self._trace) - 3000]

    # ---------- state broadcasting ----------

    def snapshot(self) -> dict:
        status = self.status
        segment = None
        if status is not None:
            segment = {
                "index": status.segment.index,
                "label": status.segment.cell_label,
                "target_watts": status.segment.target_watts,
                "target_rpm": status.segment.target_rpm,
                "elapsed_s": round(status.elapsed_s, 1),
                "remaining_s": round(status.remaining_s, 1),
                "measuring": status.measuring,
                "cadence_error": status.cadence_error,
                "cadence_in_zone": status.cadence_in_zone,
            }

        total = self.protocol.total_duration_s
        progress = 0.0
        if status is not None and total > 0:
            progress = min(1.0, status.t / total)
        elif self.phase == "finished":
            progress = 1.0

        return {
            "phase": self.phase,
            "mode": self.mode,
            "speed": self._clock.speed if self._clock else 1.0,
            "sources": {name: monitor.snapshot() for name, monitor in self.monitors.items()},
            "segment": segment,
            "progress": progress,
            "protocol": protocol_to_payload(self.protocol),
            "warnings": self.warnings[-10:],
            "error": self.error,
            "report": self.report,
            "report_text": self.report_text,
            "trace": self._trace[-600:],
            "cadence_tolerance_rpm": self.protocol.cadence_tolerance_rpm,
        }

    async def _ensure_push_task(self) -> None:
        if self._push_task is None:
            self._push_task = asyncio.create_task(self._push_loop())

    async def _push_loop(self) -> None:
        try:
            while True:
                await self._broadcast()
                await asyncio.sleep(PUSH_INTERVAL_S)
        except asyncio.CancelledError:
            pass

    async def _broadcast(self) -> None:
        if not self._sockets:
            return
        message = json.dumps({"type": "state", "state": self.snapshot()})
        for socket in list(self._sockets):
            try:
                await socket.send_str(message)
            except Exception:  # noqa: BLE001 - drop dead sockets
                self._sockets.discard(socket)

    async def shutdown(self) -> None:
        if self._push_task is not None:
            self._push_task.cancel()
            self._push_task = None
        await self.disconnect()


SERVER_KEY: web.AppKey[CompareServer] = web.AppKey("server")


def create_app(server: CompareServer | None = None) -> web.Application:
    server = server or CompareServer()
    app = web.Application()
    app[SERVER_KEY] = server

    async def index(_: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC_DIR / "index.html")

    async def get_state(request: web.Request) -> web.Response:
        return web.json_response(request.app[SERVER_KEY].snapshot())

    async def post_scan(_: web.Request) -> web.Response:
        try:
            from power_meter_audit.live.devices import scan

            devices = await scan()
        except Exception as exc:  # noqa: BLE001 - reported in the UI
            return web.json_response({"error": str(exc), "devices": []}, status=200)
        return web.json_response({"devices": devices})

    async def post_connect(request: web.Request) -> web.Response:
        payload = await request.json()
        srv: CompareServer = request.app[SERVER_KEY]
        try:
            await srv.connect(payload)
        except Exception as exc:  # noqa: BLE001 - reported in the UI
            srv.error = str(exc)
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response(srv.snapshot())

    async def post_disconnect(request: web.Request) -> web.Response:
        srv: CompareServer = request.app[SERVER_KEY]
        await srv.disconnect()
        return web.json_response(srv.snapshot())

    async def post_protocol(request: web.Request) -> web.Response:
        payload = await request.json()
        srv: CompareServer = request.app[SERVER_KEY]
        try:
            srv.protocol = protocol_from_payload(payload)
        except Exception as exc:  # noqa: BLE001 - reported in the UI
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response(protocol_to_payload(srv.protocol))

    async def post_start(request: web.Request) -> web.Response:
        srv: CompareServer = request.app[SERVER_KEY]
        try:
            await srv.start()
        except Exception as exc:  # noqa: BLE001 - reported in the UI
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response(srv.snapshot())

    async def post_stop(request: web.Request) -> web.Response:
        srv: CompareServer = request.app[SERVER_KEY]
        await srv.stop()
        return web.json_response(srv.snapshot())

    async def download_json(request: web.Request) -> web.Response:
        srv: CompareServer = request.app[SERVER_KEY]
        if srv.log is None:
            raise web.HTTPNotFound(text="no session recorded yet")
        return web.json_response(
            srv.log.to_dict(),
            headers={"Content-Disposition": 'attachment; filename="session.json"'},
        )

    async def download_csv(request: web.Request) -> web.Response:
        srv: CompareServer = request.app[SERVER_KEY]
        if srv.log is None:
            raise web.HTTPNotFound(text="no session recorded yet")
        return web.Response(
            text=srv.log.csv_text(),
            content_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="session.csv"'},
        )

    async def websocket(request: web.Request) -> web.WebSocketResponse:
        srv: CompareServer = request.app[SERVER_KEY]
        socket = web.WebSocketResponse(heartbeat=20)
        await socket.prepare(request)
        srv._sockets.add(socket)
        await srv._ensure_push_task()
        await socket.send_str(json.dumps({"type": "state", "state": srv.snapshot()}))
        try:
            async for message in socket:
                if message.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                    break
        finally:
            srv._sockets.discard(socket)
        return socket

    async def on_shutdown(app: web.Application) -> None:
        await app[SERVER_KEY].shutdown()

    app.router.add_get("/", index)
    app.router.add_get("/api/state", get_state)
    app.router.add_post("/api/scan", post_scan)
    app.router.add_post("/api/connect", post_connect)
    app.router.add_post("/api/disconnect", post_disconnect)
    app.router.add_post("/api/protocol", post_protocol)
    app.router.add_post("/api/start", post_start)
    app.router.add_post("/api/stop", post_stop)
    app.router.add_get("/api/session.json", download_json)
    app.router.add_get("/api/session.csv", download_csv)
    app.router.add_get("/ws", websocket)
    app.router.add_static("/static/", STATIC_DIR)
    app.on_shutdown.append(on_shutdown)
    return app


def serve(host: str = "127.0.0.1", port: int = 8737, open_browser: bool = True) -> None:
    app = create_app()
    url = f"http://{host}:{port}/"
    print(f"Power meter comparison UI on {url}")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 - headless is fine
            pass
    web.run_app(app, host=host, port=port, print=None)
