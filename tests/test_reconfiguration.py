from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path

import pytest

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
