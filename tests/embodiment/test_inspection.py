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
            "camera-a",
            "lineage-a",
            f"sample-{sequence}",
            sequence,
            time.monotonic(),
            4,
            4,
            bytes(4 * 4 * 3),
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
    operation = await session.act("inspect_target", {"target_id": "bench-marker"}, request_id="one")
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
        session, _, result = await _run(CameraFixture([1, 2, 3]), FailingWorker([0.1, 0.1]), light)
        assert result.lifecycle == Lifecycle.FAILED
        assert result.reason_code == "OBSERVATION_PROVIDER_FAILED"
        assert result.effect_state == EffectState.REPORTED
        assert result.result["error_type"] == "RuntimeError"
        assert light.writes == [0.25]
        assert (await session.observe()).effect_inhibited_reason is None
        await session.close()

    asyncio.run(scenario())


def test_light_transport_failure_is_explicitly_indeterminate() -> None:
    class LostLight(LightFixture):
        async def set_brightness(self, level: float) -> LightReport:
            self.writes.append(level)
            raise ConnectionError("service result was lost")

    async def scenario() -> None:
        light = LostLight()
        session, _, result = await _run(CameraFixture([1, 2]), WorkerFixture([0.1, 0.1]), light)
        assert result.lifecycle == Lifecycle.INDETERMINATE
        assert result.reason_code == "LIGHT_COMMAND_UNCONFIRMED"
        assert result.effect_state == EffectState.UNKNOWN
        assert result.result["error_type"] == "ConnectionError"
        assert light.writes == [0.25]
        assert (await session.observe()).effect_inhibited_reason == "LIGHT_COMMAND_UNCONFIRMED"
        await session.close()

    asyncio.run(scenario())


def test_buffered_pre_report_frame_cannot_verify_lighting_change() -> None:
    class BufferedCamera(CameraFixture):
        def __init__(self) -> None:
            super().__init__([1, 2, 3])
            self.buffered_at = time.monotonic()

        async def capture(self) -> CameraFrame:
            frame = await super().capture()
            if frame.sequence == 3:
                return CameraFrame(
                    frame.source_id,
                    frame.lineage_id,
                    frame.sample_id,
                    frame.sequence,
                    self.buffered_at,
                    frame.width,
                    frame.height,
                    frame.rgb8,
                )
            return frame

    async def scenario() -> None:
        light = LightFixture()
        camera = BufferedCamera()
        worker = WorkerFixture([0.1, 0.1, 0.9])
        session, _, result = await _run(camera, worker, light)
        assert result.lifecycle == Lifecycle.FAILED
        assert result.reason_code == "FRAME_BEFORE_LIGHT_REPORT"
        assert result.effect_state == EffectState.REPORTED
        assert worker.calls == 2
        assert light.writes == [0.25]
        await session.close()

    asyncio.run(scenario())


def test_primary_worker_loss_uses_one_alternate_and_rechecks_fresh_frames() -> None:
    class PrimaryWorker(WorkerFixture):
        async def detect(self, frame: CameraFrame) -> MarkerMeasurement:
            if frame.sequence >= 3:
                raise RuntimeError("primary process lost")
            return await super().detect(frame)

    class AlternateWorker(WorkerFixture):
        worker_id = "worker-b"

    async def scenario() -> None:
        camera = CameraFixture([1, 2, 3, 4, 5, 6])
        light = LightFixture()
        primary = PrimaryWorker([0.1, 0.1])
        alternate = AlternateWorker([0.8, 0.8, 0.9])
        host = OperationHost(
            (inspection_spec(camera, primary, alternate_worker=alternate, light=light),),
            runtime_id="inspection",
        )
        session = Session(host, owns_runtime=True)
        try:
            operation = await session.act(
                "inspect_target", {"target_id": "bench-marker"}, request_id="stable"
            )
            result = await session.wait(operation, 0.5)
            assert result.lifecycle == Lifecycle.SUCCEEDED
            assert result.effect_state == EffectState.REPORTED
            assert result.result["worker_id"] == "worker-b"
            assert result.result["binding_revision"] == 2
            assert result.result["worker_replacements"] == 1
            assert result.result["sample_ids"] == ("sample-5", "sample-6")
            assert alternate.calls == 3  # warmup plus two new verification frames
            assert light.writes == [0.25]
            repeated = await session.act(
                "inspect_target", {"target_id": "bench-marker"}, request_id="stable"
            )
            assert repeated.operation_id == operation.operation_id
            assert camera.captures == 6
        finally:
            await session.close()

    asyncio.run(scenario())


def test_stale_alternate_warmup_cannot_restore_inspection() -> None:
    class PrimaryWorker(WorkerFixture):
        async def detect(self, frame: CameraFrame) -> MarkerMeasurement:
            raise RuntimeError("primary process lost")

    class AlternateWorker(WorkerFixture):
        worker_id = "worker-b"

    async def scenario() -> None:
        camera = CameraFixture([1, 2])
        primary = PrimaryWorker([])
        alternate = AlternateWorker([0.9], stale_reply=True)
        host = OperationHost(
            (inspection_spec(camera, primary, alternate_worker=alternate),),
            runtime_id="inspection",
        )
        session = Session(host, owns_runtime=True)
        try:
            operation = await session.act("inspect_target", {"target_id": "bench-marker"})
            result = await session.wait(operation, 0.5)
            assert result.lifecycle == Lifecycle.FAILED
            assert result.reason_code == "WORKER_ALTERNATE_UNAVAILABLE"
            assert result.result["worker_replacements"] == 1
            assert camera.captures == 2
        finally:
            await session.close()

    asyncio.run(scenario())


def test_alternate_program_must_match_approved_primary() -> None:
    class OtherWorker(WorkerFixture):
        worker_id = "worker-b"
        program_id = "other-program"

    with pytest.raises(ValueError, match="approved program"):
        inspection_spec(CameraFixture([]), WorkerFixture([]), alternate_worker=OtherWorker([]))


@pytest.mark.parametrize(
    ("delays", "qualities", "primary_fails", "reason", "writes"),
    [
        ([0.006, 0.006], [0.9, 0.9], False, "FRAME_STALE", []),
        ([0.06], [0.9], False, "FRAME_STALE", []),
        ([0.06], [0.1], False, "FRAME_STALE", []),
        ([0.001, 0.001, 0.06], [0.1, 0.1, 0.9], False, "FRAME_STALE", [0.25]),
        ([0.06], [0.9], True, "WORKER_ALTERNATE_UNAVAILABLE", []),
        ([0.001, 0.001, 0.06], [0.9, 0.9, 0.9], True, "FRAME_STALE", []),
    ],
)
def test_expired_compute_evidence_cannot_succeed_or_adjust_light(
    monkeypatch: pytest.MonkeyPatch,
    delays: list[float],
    qualities: list[float],
    primary_fails: bool,
    reason: str,
    writes: list[float],
) -> None:
    from dataclasses import replace
    from types import SimpleNamespace

    from entryplug.embodiment import inspection

    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(inspection, "time", SimpleNamespace(monotonic=lambda: clock.now))

    class ClockCamera(CameraFixture):
        async def capture(self) -> CameraFrame:
            clock.now += 0.0001  # strictly after a reported lighting boundary
            return replace(await super().capture(), received_monotonic=clock.now)

    class ClockWorker(WorkerFixture):
        def __init__(self) -> None:
            super().__init__(qualities)
            self.delays = iter(delays)

        async def detect(self, frame: CameraFrame) -> MarkerMeasurement:
            result = await super().detect(frame)
            clock.now += next(self.delays)
            return result

    class DeadWorker(WorkerFixture):
        async def detect(self, frame: CameraFrame) -> MarkerMeasurement:
            raise ConnectionError("primary retired")

    async def scenario() -> None:
        light = LightFixture()
        worker = ClockWorker()
        if primary_fails:
            worker.worker_id = "worker-b"
        host = OperationHost(
            (
                inspection_spec(
                    ClockCamera(list(range(1, 20))),
                    DeadWorker([]) if primary_fails else worker,
                    alternate_worker=worker if primary_fails else None,
                    light=light,
                    config=InspectionConfig(maximum_frame_age_s=0.01),
                ),
            )
        )
        async with Session(host, owns_runtime=True) as session:
            operation = await session.act("inspect_target", {"target_id": "bench-marker"})
            result = await session.wait(operation, 1)
            assert result.lifecycle == Lifecycle.FAILED
            assert result.reason_code == reason
            assert light.writes == writes
            assert result.effect_state == (EffectState.REPORTED if writes else EffectState.NONE)

    asyncio.run(scenario())


@pytest.mark.parametrize("deadline", [False, True])
def test_stop_while_compute_is_pending_prevents_late_success_and_writes(deadline: bool) -> None:
    from dataclasses import replace

    class PendingWorker(WorkerFixture):
        def __init__(self) -> None:
            super().__init__([0.1, 0.1])
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def detect(self, frame: CameraFrame) -> MarkerMeasurement:
            self.started.set()
            await self.release.wait()
            return await super().detect(frame)

    async def scenario() -> None:
        worker = PendingWorker()
        camera = CameraFixture([1, 2])
        light = LightFixture()
        spec = inspection_spec(camera, worker, light=light)
        if deadline:
            spec = replace(spec, deadline_seconds=0.01)
        host = OperationHost((spec,))
        async with Session(host, owns_runtime=True) as session:
            operation = await session.act("inspect_target", {"target_id": "bench-marker"})
            await worker.started.wait()
            if deadline:
                # Observe the host's deadline event, not a guessed worker delay.
                await asyncio.wait_for(
                    host._operation(operation.operation_id).cancel_event.wait(), 1
                )
            else:
                await session.cancel(operation)
            worker.release.set()
            result = await session.wait(operation, 1)
            assert result.lifecycle == (Lifecycle.FAILED if deadline else Lifecycle.CANCELED)
            assert result.reason_code == ("DEADLINE_EXCEEDED" if deadline else "CANCEL_REQUESTED")
            assert camera.captures == 1
            assert light.writes == []
            assert result.effect_state == EffectState.NONE
            assert (await session.wait(operation, 0)).lifecycle == result.lifecycle

    asyncio.run(scenario())


def test_cancel_during_light_write_keeps_late_report_but_stops_adaptation() -> None:
    async def scenario() -> None:
        entered, release = asyncio.Event(), asyncio.Event()

        class HeldLight(LightFixture):
            async def set_brightness(self, level):
                entered.set()
                await release.wait()
                return await super().set_brightness(level)

        camera = CameraFixture([1, 2])
        worker = WorkerFixture([0.1, 0.2])
        light = HeldLight()
        session = Session(
            OperationHost([inspection_spec(camera, worker, light=light)]), owns_runtime=True
        )
        try:
            operation = await session.act("inspect_target", {"target_id": "bench-marker"})
            await asyncio.wait_for(entered.wait(), 1)
            await session.cancel(operation)
            release.set()
            result = await session.wait(operation, 1)
            assert result.lifecycle == Lifecycle.CANCELED
            assert result.effect_state == EffectState.REPORTED
            assert result.reason_code == "CANCEL_REQUESTED"
            assert result.result["last_reported_level"] == 0.25
            assert light.writes == [0.25]
            assert camera.captures == 2  # No post-cancel sensing or further adjustment.
            assert await session.cancel(operation) == result
        finally:
            release.set()
            await session.close()

    asyncio.run(scenario())
