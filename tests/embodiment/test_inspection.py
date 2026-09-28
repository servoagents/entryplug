"""Portable task qualification before wiring real camera, HA, or Zenoh adapters."""

from __future__ import annotations

import asyncio
import time

import pytest

from entryplug.core.operation import AdmissionError, EffectState, Lifecycle, OperationHost
from entryplug.embodiment.inspection import (
    CameraFrame,
    InspectionConfig,
    LightReport,
    MarkerMeasurement,
    inspection_spec,
)
from entryplug.harness.session import Session


class CameraFixture:
    def __init__(self, sequences: list[int]) -> None:
        self.sequences = iter(sequences)
        self.captures = 0

    async def capture(self) -> CameraFrame:
        sequence = next(self.sequences)
        self.captures += 1
        return CameraFrame(
            "camera-a", "lineage-a", f"sample-{sequence}", sequence,
            time.monotonic(), 4, 4, bytes(4 * 4 * 3),
        )


class WorkerFixture:
    worker_id = "worker-a"
    program_id = "red-marker-v1"

    def __init__(self, qualities: list[float], *, stale_reply: bool = False) -> None:
        self.qualities = iter(qualities)
        self.stale_reply = stale_reply
        self.calls = 0

    async def detect(self, frame: CameraFrame) -> MarkerMeasurement:
        self.calls += 1
        return MarkerMeasurement(
            frame.source_id,
            "sample-old" if self.stale_reply else frame.sample_id,
            self.worker_id,
            self.program_id,
            1.0,
            2.0,
            next(self.qualities),
        )


class LightFixture:
    entity_id = "fixture-light"

    def __init__(self, *, confirm: bool = True) -> None:
        self.confirm = confirm
        self.writes: list[float] = []

    async def set_brightness(self, level: float) -> LightReport:
        self.writes.append(level)
        return LightReport(self.entity_id, level if self.confirm else None, self.confirm)


async def _run(camera: CameraFixture, worker: WorkerFixture, light: LightFixture | None):
    host = OperationHost((inspection_spec(camera, worker, light=light),), runtime_id="inspection")
    session = Session(host, owns_runtime=True)
    operation = await session.act(
        "inspect_target", {"target_id": "bench-marker"}, request_id="one"
    )
    result = await session.wait(operation, 0.5)
    return session, operation, result


def test_adequate_observation_needs_no_light_write() -> None:
    async def scenario() -> None:
        camera = CameraFixture([1, 2])
        worker = WorkerFixture([0.8, 0.9])
        light = LightFixture()
        session, operation, result = await _run(camera, worker, light)
        assert result.lifecycle == Lifecycle.SUCCEEDED
        assert result.effect_state == EffectState.NONE
        assert result.result["sample_ids"] == ("sample-1", "sample-2")
        assert result.result["lighting_writes"] == 0
        assert result.result["worker_id"] == worker.worker_id
        assert light.writes == []
        repeated = await session.act(
            "inspect_target", {"target_id": "bench-marker"}, request_id="one"
        )
        assert repeated.operation_id == operation.operation_id
        assert camera.captures == 2
        await session.close()

    asyncio.run(scenario())


def test_low_quality_uses_one_light_level_then_two_new_frames() -> None:
    async def scenario() -> None:
        camera = CameraFixture([1, 2, 3, 4])
        worker = WorkerFixture([0.1, 0.2, 0.8, 0.9])
        light = LightFixture()
        session, _, result = await _run(camera, worker, light)
        assert result.lifecycle == Lifecycle.SUCCEEDED
        assert result.effect_state == EffectState.REPORTED
        assert result.result["sample_ids"] == ("sample-3", "sample-4")
        assert result.result["last_sequence"] == 4
        assert result.result["last_reported_level"] == 0.25
        assert light.writes == [0.25]
        await session.close()

    asyncio.run(scenario())


def test_read_only_task_refuses_dark_target_without_side_effects() -> None:
    async def scenario() -> None:
        camera = CameraFixture([1, 2])
        worker = WorkerFixture([0.1, 0.2])
        session, _, result = await _run(camera, worker, None)
        assert result.lifecycle == Lifecycle.FAILED
        assert result.reason_code == "OBSERVATION_UNAVAILABLE"
        assert result.effect_state == EffectState.NONE
        assert result.result["lighting_writes"] == 0
        catalog = (await session.observe()).capabilities[0]
        assert catalog["physical_effects"] is False
        await session.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("sequences", "stale_reply", "reason"),
    [
        ([1, 1], False, "FRAME_NOT_PROGRESSING"),
        ([1, 2], True, "WORKER_REPLY_MISMATCH"),
    ],
)
def test_stale_capture_or_worker_reply_cannot_become_evidence(
    sequences: list[int], stale_reply: bool, reason: str
) -> None:
    async def scenario() -> None:
        light = LightFixture()
        session, _, result = await _run(
            CameraFixture(sequences), WorkerFixture([0.9, 0.9], stale_reply=stale_reply), light
        )
        assert result.lifecycle == Lifecycle.FAILED
        assert result.reason_code == reason
        assert result.effect_state == EffectState.NONE
        assert light.writes == []
        await session.close()

    asyncio.run(scenario())


def test_old_frame_after_light_cannot_pass_verification() -> None:
    async def scenario() -> None:
        light = LightFixture()
        session, _, result = await _run(
            CameraFixture([1, 2, 2]), WorkerFixture([0.1, 0.1, 0.9]), light
        )
        assert result.lifecycle == Lifecycle.FAILED
        assert result.reason_code == "FRAME_NOT_PROGRESSING"
        assert result.effect_state == EffectState.REPORTED
        assert light.writes == [0.25]
        await session.close()

    asyncio.run(scenario())


def test_unconfirmed_light_state_stops_further_writes_and_inhibits_effects() -> None:
    async def scenario() -> None:
        light = LightFixture(confirm=False)
        camera = CameraFixture([1, 2])
        host = OperationHost(
            (inspection_spec(camera, WorkerFixture([0.1, 0.1]), light=light),),
            runtime_id="inspection",
        )
        session = Session(host, owns_runtime=True)
        operation = await session.act("inspect_target", {"target_id": "bench-marker"})
        result = await session.wait(operation, 0.5)
        assert result.lifecycle == Lifecycle.INDETERMINATE
        assert result.reason_code == "LIGHT_STATE_UNCONFIRMED"
        assert result.effect_state == EffectState.UNKNOWN
        assert light.writes == [0.25]
        assert camera.captures == 2
        assert (await session.observe()).effect_inhibited_reason == "LIGHT_STATE_UNCONFIRMED"
        await session.close()

    asyncio.run(scenario())


def test_fixture_configuration_caps_light_levels_and_rejects_other_targets() -> None:
    with pytest.raises(ValueError, match="at most three"):
        InspectionConfig(brightness_levels=(0.25, 0.5, 0.75, 0.8))

    async def scenario() -> None:
        host = OperationHost(
            (inspection_spec(CameraFixture([]), WorkerFixture([])),), runtime_id="inspection"
        )
        session = Session(host, owns_runtime=True)
        with pytest.raises(AdmissionError, match="configured target"):
            await session.act("inspect_target", {"target_id": "other"})
        await session.close()

    asyncio.run(scenario())


def test_worker_failure_after_reported_light_keeps_known_effect_state() -> None:
    class FailingWorker(WorkerFixture):
        async def detect(self, frame: CameraFrame) -> MarkerMeasurement:
            if frame.sequence >= 3:
                raise RuntimeError("worker went away")
            return await super().detect(frame)

    async def scenario() -> None:
        light = LightFixture()
        session, _, result = await _run(
            CameraFixture([1, 2, 3]), FailingWorker([0.1, 0.1]), light
        )
        assert result.lifecycle == Lifecycle.FAILED
        assert result.reason_code == "OBSERVATION_PROVIDER_FAILED"
        assert result.effect_state == EffectState.REPORTED
        assert result.result["error_type"] == "RuntimeError"
        assert light.writes == [0.25]
        assert (await session.observe()).effect_inhibited_reason is None
        await session.close()

    asyncio.run(scenario())
