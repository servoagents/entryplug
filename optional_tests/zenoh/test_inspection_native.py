"""Task-level Session proof with real native Zenoh worker processes.

The camera and light here are in-memory fixtures. This does not qualify the
rendered panel or the Home Assistant/MQTT route.
"""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import socket
import time
from typing import Any, cast

import numpy as np
import zenoh

from entryplug.core.operation import EffectState, Lifecycle, OperationHost
from entryplug.embodiment.inspection import CameraFrame, LightReport, inspection_spec
from entryplug.harness.session import Session
from entryplug_zenoh.detector import NativeDetectorServer, ZenohDetectorWorker


def _port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _config(mode: str, port: int) -> zenoh.Config:
    address = f"tcp/127.0.0.1:{port}"
    endpoint = {"listen" if mode == "peer" else "connect": {"endpoints": [address]}}
    return zenoh.Config.from_json5(
        json.dumps({"mode": mode, "scouting": {"multicast": {"enabled": False}}, **endpoint})
    )


def _server(
    port: int,
    worker_id: str,
    ready: multiprocessing.synchronize.Event,
    stop: multiprocessing.synchronize.Event,
) -> None:
    with zenoh.open(_config("peer", port)) as session:
        worker = NativeDetectorServer(session, run_id="task-run", worker_id=worker_id, generation=1)
        ready.set()
        try:
            stop.wait(15.0)
        finally:
            worker.close()


class _Camera:
    def __init__(self, *, illuminated: bool) -> None:
        self.illuminated = illuminated
        self.sequence = 0

    async def capture(self) -> CameraFrame:
        self.sequence += 1
        rgb = np.zeros((240, 320, 3), dtype=np.uint8)
        if self.illuminated:
            rgb[100:120, 145:175] = (200, 0, 0)
        return CameraFrame(
            "camera-a",
            "capture-a",
            f"sample-{self.sequence}",
            self.sequence,
            time.monotonic(),
            320,
            240,
            rgb.tobytes(),
        )


class _Light:
    entity_id = "fixture-light"

    def __init__(self, camera: _Camera, failed_process: multiprocessing.Process) -> None:
        self.camera = camera
        self.failed_process = failed_process
        self.writes: list[float] = []

    async def set_brightness(self, level: float) -> LightReport:
        self.writes.append(level)
        # Evaluator-only process death after the first dark observation pair.
        self.failed_process.terminate()
        await asyncio.to_thread(self.failed_process.join, 3.0)
        assert self.failed_process.exitcode is not None
        self.camera.illuminated = True
        return LightReport(self.entity_id, level, True)


def _start_worker(
    context: Any, worker_id: str
) -> tuple[int, multiprocessing.Process, multiprocessing.synchronize.Event]:
    port = _port()
    ready, stop = context.Event(), context.Event()
    process = context.Process(target=_server, args=(port, worker_id, ready, stop))
    process.start()
    assert ready.wait(6.0), f"{worker_id} did not declare its queryable"
    return port, process, stop


def _stop(process: multiprocessing.Process, stop: multiprocessing.synchronize.Event) -> None:
    if process.is_alive():
        stop.set()
        process.join(3.0)
    if process.is_alive():
        process.terminate()
        process.join(2.0)


def test_public_session_reuses_native_worker_for_two_fresh_tasks() -> None:
    context = multiprocessing.get_context("spawn")
    port, process, stop = _start_worker(context, "worker-a")
    try:
        with zenoh.open(_config("client", port)) as native:
            worker = ZenohDetectorWorker(
                native, run_id="task-run", worker_id="worker-a", generation=1
            )
            camera = _Camera(illuminated=True)

            async def scenario() -> None:
                host = OperationHost((inspection_spec(camera, worker),), runtime_id="task-run")
                session = Session(host, owns_runtime=True)
                try:
                    results = []
                    for request_id in ("first", "second"):
                        operation = await session.act(
                            "inspect_target", {"target_id": "bench-marker"}, request_id=request_id
                        )
                        results.append(await session.wait(operation, 5.0))
                    assert all(result.lifecycle == Lifecycle.SUCCEEDED for result in results)
                    assert [
                        cast(tuple[str, str], result.result["sample_ids"]) for result in results
                    ] == [
                        ("sample-1", "sample-2"),
                        ("sample-3", "sample-4"),
                    ]
                    assert all(result.result["lighting_writes"] == 0 for result in results)
                    assert camera.sequence == 4
                finally:
                    await session.close()

            asyncio.run(scenario())
            worker.close()
    finally:
        _stop(process, stop)
        assert process.exitcode == 0


def test_native_primary_loss_repairs_once_under_same_task_identity() -> None:
    context = multiprocessing.get_context("spawn")
    primary_port, primary_process, primary_stop = _start_worker(context, "worker-a")
    alternate_port, alternate_process, alternate_stop = _start_worker(context, "worker-b")
    try:
        with (
            zenoh.open(_config("client", primary_port)) as primary_session,
            zenoh.open(_config("client", alternate_port)) as alternate_session,
        ):
            primary = ZenohDetectorWorker(
                primary_session, run_id="task-run", worker_id="worker-a", generation=1
            )
            alternate = ZenohDetectorWorker(
                alternate_session, run_id="task-run", worker_id="worker-b", generation=1
            )
            camera = _Camera(illuminated=False)
            light = _Light(camera, primary_process)

            async def scenario() -> None:
                host = OperationHost(
                    (inspection_spec(camera, primary, alternate_worker=alternate, light=light),),
                    runtime_id="task-run",
                )
                session = Session(host, owns_runtime=True)
                try:
                    operation = await session.act(
                        "inspect_target", {"target_id": "bench-marker"}, request_id="stable"
                    )
                    result = await session.wait(operation, 10.0)
                    assert result.lifecycle == Lifecycle.SUCCEEDED
                    assert result.effect_state == EffectState.REPORTED
                    assert result.result["worker_id"] == "worker-b"
                    assert cast(tuple[str, str], result.result["sample_ids"]) == (
                        "sample-5",
                        "sample-6",
                    )
                    assert result.result["binding_revision"] == 2
                    assert result.result["worker_replacements"] == 1
                    assert light.writes == [0.25]
                    repeated = await session.act(
                        "inspect_target", {"target_id": "bench-marker"}, request_id="stable"
                    )
                    assert repeated.operation_id == operation.operation_id
                    assert camera.sequence == 6
                finally:
                    await session.close()

            asyncio.run(scenario())
            primary.close()
            alternate.close()
    finally:
        _stop(primary_process, primary_stop)
        _stop(alternate_process, alternate_stop)
        assert alternate_process.exitcode == 0
