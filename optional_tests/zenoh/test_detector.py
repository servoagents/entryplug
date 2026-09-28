"""Actual loopback Zenoh request/reply with a separate approved detector process."""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import socket
import time
from types import SimpleNamespace

import numpy as np
import pytest
import zenoh

from entryplug.embodiment.inspection import CameraFrame
from entryplug_zenoh.detector import (
    NativeDetectorError,
    NativeDetectorServer,
    ZenohDetectorWorker,
    detector_key,
)


def _config(mode: str, port: int) -> zenoh.Config:
    address = f"tcp/127.0.0.1:{port}"
    endpoint = {"listen" if mode == "peer" else "connect": {"endpoints": [address]}}
    return zenoh.Config.from_json5(
        json.dumps({"mode": mode, "scouting": {"multicast": {"enabled": False}}, **endpoint})
    )


def _server(
    port: int, ready: multiprocessing.synchronize.Event, stop: multiprocessing.synchronize.Event
) -> None:
    with zenoh.open(_config("peer", port)) as session:
        worker = NativeDetectorServer(
            session, run_id="test-run", worker_id="worker-a", generation=1
        )
        ready.set()
        try:
            stop.wait(10.0)
        finally:
            worker.close()


def _port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _frame() -> CameraFrame:
    rgb = np.zeros((240, 320, 3), dtype=np.uint8)
    rgb[100:120, 145:175] = (200, 0, 0)
    return CameraFrame(
        "camera-a",
        "capture-a",
        "sample-7",
        7,
        time.monotonic(),
        320,
        240,
        rgb.tobytes(),
    )


def test_exact_key_and_generation_validation() -> None:
    assert detector_key("run-1", "worker-a").endswith("/run-1/workers/worker-a/detect")
    with pytest.raises(ValueError):
        detector_key("run/*", "worker-a")
    with pytest.raises(ValueError):
        detector_key("run-1", "../worker")


def test_separate_native_process_returns_measured_marker() -> None:
    context = multiprocessing.get_context("spawn")
    ready, stop = context.Event(), context.Event()
    port = _port()
    process = context.Process(target=_server, args=(port, ready, stop))
    process.start()
    try:
        assert ready.wait(6.0), "native worker did not declare its queryable"
        with zenoh.open(_config("client", port)) as session:
            worker = ZenohDetectorWorker(
                session, run_id="test-run", worker_id="worker-a", generation=1
            )
            try:
                result = asyncio.run(worker.detect(_frame()))
            finally:
                worker.close()
        assert result.source_id == "camera-a"
        assert result.sample_id == "sample-7"
        assert result.worker_id == "worker-a"
        assert result.program_id == "fixed-red-centroid-v1"
        assert (result.centroid_x, result.centroid_y, result.quality) == (159.5, 109.5, 0.6)
    finally:
        stop.set()
        process.join(5.0)
        if process.is_alive():
            process.terminate()
            process.join(2.0)
        assert process.exitcode == 0


def test_wrong_worker_identity_is_rejected_without_fallback() -> None:
    class WrongReply:
        ok = type(
            "Sample",
            (),
            {
                "key_expr": detector_key("test-run", "worker-a"),
                "payload": type("Bytes", (), {"to_bytes": lambda self: b'{"worker_id":"other"}'})(),
            },
        )()

    class WrongSession:
        def get(self, *_: object, **__: object) -> list[WrongReply]:
            return [WrongReply()]

    worker = ZenohDetectorWorker(
        WrongSession(), run_id="test-run", worker_id="worker-a", generation=1
    )

    async def scenario() -> None:
        with pytest.raises(NativeDetectorError, match="identity"):
            await worker.detect(_frame())
        with pytest.raises(NativeDetectorError, match="replacement"):
            await worker.detect(_frame())

    asyncio.run(scenario())


def test_transport_failure_poisoned_instance_does_not_dispatch_again() -> None:
    class LostSession:
        calls = 0

        def get(self, *_: object, **__: object) -> object:
            self.calls += 1
            raise ConnectionError("query result lost")

    session = LostSession()
    worker = ZenohDetectorWorker(session, run_id="test-run", worker_id="worker-a", generation=1)

    async def scenario() -> None:
        with pytest.raises(NativeDetectorError, match="query failed"):
            await worker.detect(_frame())
        with pytest.raises(NativeDetectorError, match="replacement"):
            await worker.detect(_frame())

    asyncio.run(scenario())
    assert session.calls == 1


def test_boolean_generation_cannot_match_integer_worker_identity() -> None:
    class BytesValue:
        def __init__(self, data: bytes) -> None:
            self.data = data

        def to_bytes(self) -> bytes:
            return self.data

    class BooleanGenerationSession:
        def get(self, key: str, *, attachment: bytes, **_: object) -> list[SimpleNamespace]:
            result = json.loads(attachment)
            result.update(
                worker_id="worker-a",
                worker_generation=True,
                program_id="fixed-red-centroid-v1",
                centroid_x=None,
                centroid_y=None,
                area_px=0,
            )
            sample = SimpleNamespace(key_expr=key, payload=BytesValue(json.dumps(result).encode()))
            return [SimpleNamespace(ok=sample)]

    worker = ZenohDetectorWorker(
        BooleanGenerationSession(), run_id="test-run", worker_id="worker-a", generation=1
    )

    async def scenario() -> None:
        with pytest.raises(NativeDetectorError, match="identity types"):
            await worker.detect(_frame())

    asyncio.run(scenario())
