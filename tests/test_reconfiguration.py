from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

from entryplug.agent import AgentRunner, Stop
from entryplug.detection import (
    DETECTOR_INTERFACE_VERSION,
    MarkerDetection,
    MarkerDetectorSlot,
)
from entryplug.evidence import (
    EvidenceQuery,
    EvidenceRecord,
    JsonValue,
    MemoryEvidenceCache,
    SqliteEvidenceCache,
)
from entryplug.operation import (
    AdmissionError,
    CapabilitySpec,
    Lifecycle,
    MotionState,
    OperationContext,
    OperationHost,
    OperationResult,
)
from entryplug.reconfiguration import EvidenceCacheSlot, ReplacementError


class _StopPolicy:
    def __init__(self, reason: str) -> None:
        self._reason = reason

    def decide(self, _: Mapping[str, JsonValue]) -> Stop:
        return Stop(self._reason)


@dataclass(frozen=True)
class _Frame:
    marker_x: int
    width: int = 32
    height: int = 24
    encoding: str = "rgb8"
    step: int = 96
    data: bytes = b""


class _Detector:
    interface_version = DETECTOR_INTERFACE_VERSION

    def __init__(self, detector_id: str, offset: int) -> None:
        self.detector_id = detector_id
        self._offset = offset
        self.closed = False

    def prepare(self) -> None:
        return None

    def detect(self, frame: _Frame) -> MarkerDetection:
        if self.closed:
            raise RuntimeError("retired detector cannot serve observations")
        x_px = frame.marker_x + self._offset
        return MarkerDetection(
            x_px=float(x_px),
            y_px=8.0,
            area_px=25,
            left_px=x_px - 2,
            top_px=6,
            right_px=x_px + 2,
            bottom_px=10,
        )

    def close(self) -> None:
        self.closed = True


def _record(created_at: str) -> EvidenceRecord:
    return EvidenceRecord.create(
        kind="visual_binding.v1",
        context={"task": "visual_reach", "model_version": "scalar-jacobian-v1"},
        payload={"source_id": created_at},
        evidence_refs=(f"runs/{created_at}/trace.jsonl",),
        created_at=created_at,
    )


def _query() -> EvidenceQuery:
    record = _record("2026-09-25T10:00:00Z")
    return EvidenceQuery(record.kind, record.context)


class FailingStoreCache(MemoryEvidenceCache):
    def __init__(self) -> None:
        super().__init__()
        self.released = False

    def store(self, record: EvidenceRecord) -> str:
        raise OSError("fixture write failed")

    def close(self) -> None:
        self.released = True
        super().close()


class FinalDeltaCache(MemoryEvidenceCache):
    def __init__(self, final_record: EvidenceRecord) -> None:
        super().__init__()
        self._final_record = final_record
        self._find_calls = 0

    def find(self, query: EvidenceQuery, limit: int = 10) -> tuple[EvidenceRecord, ...]:
        self._find_calls += 1
        if self._find_calls == 2:
            self.store(self._final_record)
        return super().find(query, limit)


def test_memory_cache_replaces_with_sqlite_and_preserves_selected_evidence(
    tmp_path: Path,
) -> None:
    host = OperationHost((), runtime_id="runtime-a")
    retired = MemoryEvidenceCache()
    slot = EvidenceCacheSlot(host, retired)
    older = _record("2026-09-25T10:00:00Z")
    newer = _record("2026-09-25T10:01:00Z")
    slot.store(older)
    slot.store(newer)

    result = slot.replace(
        SqliteEvidenceCache(tmp_path / "replacement.sqlite3"),
        expected_generation=1,
        transfer_queries=(_query(),),
    )

    assert result.previous_generation == 1
    assert result.generation == 2
    assert result.transferred_records == 2
    assert result.retired_released
    assert result.previous_implementation.endswith("MemoryEvidenceCache")
    assert result.implementation.endswith("SqliteEvidenceCache")
    assert slot.find(_query()) == (newer, older)
    assert slot.load(older.key) == older
    assert slot.observe().generation == 2
    assert host.observe().admission_open
    assert host.observe().reconfiguration_generations == {"memory": 2}
    with pytest.raises(RuntimeError, match="closed"):
        retired.find(_query())
    slot.close()


def test_replacement_copies_a_final_delta_inside_the_barrier(tmp_path: Path) -> None:
    initial = _record("2026-09-25T10:00:00Z")
    final = _record("2026-09-25T10:01:00Z")
    source = FinalDeltaCache(final)
    source.store(initial)
    slot = EvidenceCacheSlot(OperationHost(()), source)

    result = slot.replace(
        SqliteEvidenceCache(tmp_path / "replacement.sqlite3"),
        expected_generation=1,
        transfer_queries=(_query(),),
    )

    assert result.transferred_records == 2
    assert slot.find(_query()) == (final, initial)
    slot.close()


def test_failed_transfer_keeps_old_cache_and_generation() -> None:
    host = OperationHost((), runtime_id="runtime-a")
    slot = EvidenceCacheSlot(host, MemoryEvidenceCache())
    record = _record("2026-09-25T10:00:00Z")
    slot.store(record)
    replacement = FailingStoreCache()

    with pytest.raises(ReplacementError) as failed:
        slot.replace(
            replacement,
            expected_generation=1,
            transfer_queries=(_query(),),
        )

    assert failed.value.reason_code == "TRANSFER_FAILED"
    assert replacement.released
    assert slot.generation == 1
    assert slot.load(record.key) == record
    assert host.observe().admission_open
    slot.close()


def test_stale_generation_releases_replacement_without_touching_active_cache() -> None:
    slot = EvidenceCacheSlot(OperationHost(()), MemoryEvidenceCache())
    replacement = MemoryEvidenceCache()

    with pytest.raises(ReplacementError) as stale:
        slot.replace(replacement, expected_generation=0)

    assert stale.value.reason_code == "STALE_GENERATION"
    assert slot.generation == 1
    with pytest.raises(RuntimeError, match="closed"):
        replacement.find(_query())
    slot.close()


def test_active_operation_refuses_replacement_and_releases_candidate() -> None:
    async def scenario() -> None:
        release = asyncio.Event()

        def validate(arguments: Mapping[str, JsonValue]) -> Mapping[str, object]:
            return dict(arguments)

        async def run(
            _: OperationContext, __: Mapping[str, JsonValue]
        ) -> OperationResult:
            await release.wait()
            return OperationResult(Lifecycle.SUCCEEDED, MotionState.IDLE)

        host = OperationHost(
            (CapabilitySpec("read", "1", "Read", False, 1.0, 0.1, validate, run),),
            runtime_id="runtime-a",
        )
        slot = EvidenceCacheSlot(host, MemoryEvidenceCache())
        operation = await host.start(
            "read", {}, request_id="active", expected_runtime_id="runtime-a"
        )
        replacement = MemoryEvidenceCache()

        with pytest.raises(AdmissionError) as busy:
            slot.replace(replacement, expected_generation=1)

        assert busy.value.reason_code == "BUSY"
        assert slot.generation == 1
        with pytest.raises(RuntimeError, match="closed"):
            replacement.find(_query())

        release.set()
        assert (await host.wait(operation.operation_id, 0.2)).lifecycle == Lifecycle.SUCCEEDED
        slot.close()
        await host.close()

    asyncio.run(scenario())


def test_read_only_cache_is_rejected_during_preparation(tmp_path: Path) -> None:
    path = tmp_path / "read-only.sqlite3"
    writable = SqliteEvidenceCache(path)
    writable.close()
    replacement = SqliteEvidenceCache(path, read_only=True)
    slot = EvidenceCacheSlot(OperationHost(()), MemoryEvidenceCache())

    with pytest.raises(ReplacementError) as rejected:
        slot.replace(replacement, expected_generation=1)

    assert rejected.value.reason_code == "READ_ONLY_REPLACEMENT"
    assert slot.generation == 1
    with pytest.raises(RuntimeError, match="closed"):
        replacement.find(_query())
    slot.close()


def test_controlled_replacements_preserve_operation_ledger_and_request_identity(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        calls = 0

        def validate(arguments: Mapping[str, JsonValue]) -> Mapping[str, object]:
            if set(arguments) != {"value"} or not isinstance(arguments["value"], int):
                raise ValueError("value must be one integer")
            return dict(arguments)

        async def record(
            _: OperationContext, arguments: Mapping[str, JsonValue]
        ) -> OperationResult:
            nonlocal calls
            calls += 1
            return OperationResult(
                Lifecycle.SUCCEEDED,
                MotionState.IDLE,
                {"recorded": arguments["value"]},
            )

        host = OperationHost(
            (CapabilitySpec("record", "1", "Record", False, 1.0, 0.1, validate, record),),
            runtime_id="runtime-a",
        )
        accepted = await host.start(
            "record",
            {"value": 17},
            request_id="stable-request",
            expected_runtime_id="runtime-a",
        )
        completed = await host.wait(accepted.operation_id, 0.2)
        original_history = host.observe().operations

        retired_cache = MemoryEvidenceCache()
        cache_slot = EvidenceCacheSlot(host, retired_cache)
        cached = _record("2026-09-25T10:02:00Z")
        cache_slot.store(cached)

        detectors: list[_Detector] = []

        def detector_factory(detector_id: str, offset: int):
            def create() -> _Detector:
                detector = _Detector(detector_id, offset)
                detectors.append(detector)
                return detector

            return create

        detector_slot = MarkerDetectorSlot(
            host,
            {
                "baseline": detector_factory("baseline", 0),
                "replacement": detector_factory("replacement", 3),
            },
            initial_detector_id="baseline",
        )
        frame = _Frame(marker_x=10)
        runner = AgentRunner(_StopPolicy("before replacement"))
        before_policy = await runner.decide(host.observe())

        cache_replacement = cache_slot.replace(
            SqliteEvidenceCache(tmp_path / "replacement.sqlite3"),
            expected_generation=1,
            transfer_queries=(_query(),),
        )
        detector_replacement = detector_slot.select(
            "replacement",
            expected_generation=1,
            validation_frame=frame,
        )
        policy_replacement = await runner.replace(
            _StopPolicy("after replacement"),
            host=host,
            expected_generation=1,
        )

        repeated = await host.start(
            "record",
            {"value": 17},
            request_id="stable-request",
            expected_runtime_id="runtime-a",
        )
        after_policy = await runner.decide(host.observe())
        current = host.observe()

        assert completed.lifecycle == Lifecycle.SUCCEEDED
        assert completed.result == {"recorded": 17}
        assert current.operations == original_history
        assert repeated.operation_id == completed.operation_id
        assert repeated.result == completed.result
        assert calls == 1
        assert current.request_records == 1
        assert current.reconfiguration_generations == {
            "agent_policy": 2,
            "detector": 2,
            "memory": 2,
        }

        assert cache_replacement.retired_released
        assert cache_slot.load(cached.key) == cached
        with pytest.raises(RuntimeError, match="closed"):
            retired_cache.find(_query())

        assert detector_replacement.retired_released
        assert detectors[0].closed
        reading = detector_slot.detect(frame)
        assert reading.detector_id == "replacement"
        assert reading.generation == 2
        assert reading.marker.x_px == 13.0

        assert policy_replacement.previous_process_id == before_policy.agent_process_id
        assert policy_replacement.process_id == after_policy.agent_process_id
        assert after_policy.agent_process_id != before_policy.agent_process_id
        assert after_policy.decision == Stop("after replacement")

        await runner.close()
        detector_slot.close()
        cache_slot.close()
        await host.close()

    asyncio.run(scenario())
