from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from entryplug.evidence import (
    EvidenceCache,
    EvidenceQuery,
    EvidenceRecord,
    MemoryEvidenceCache,
    SqliteEvidenceCache,
)


def _record(created_at: str = "2026-09-23T12:00:00+00:00") -> EvidenceRecord:
    return EvidenceRecord.create(
        kind="visual_binding.v1",
        context={
            "task": "visual_reach",
            "model_version": "scalar-jacobian-v1",
            "source_convention": "red-centroid-y",
        },
        payload={
            "source_id": "view-a",
            "lineage_id": "lineage-a",
            "jacobian": {"shape": [1, 1], "values": [96.0]},
            "command_unit": "radian",
            "output_unit": "pixel",
            "validity_radians": [-0.05, 0.05],
        },
        evidence_refs=("run-1/mirrors.json", "run-1/trace.jsonl"),
        created_at=created_at,
    )


@pytest.fixture(params=("memory", "sqlite"))
def cache_factory(request: pytest.FixtureRequest, tmp_path: Path) -> Callable[[], EvidenceCache]:
    if request.param == "memory":
        return MemoryEvidenceCache
    return lambda: SqliteEvidenceCache(tmp_path / "evidence.sqlite3")


def test_cache_implementations_share_round_trip_and_query_contract(
    cache_factory: Callable[[], EvidenceCache],
) -> None:
    cache = cache_factory()
    older = _record()
    newer = _record("2026-09-23T12:01:00+00:00")
    query = EvidenceQuery(kind=older.kind, context=older.context)

    assert cache.store(older) == older.key
    assert cache.store(newer) == newer.key
    assert cache.store(older) == older.key
    assert cache.load(older.key) == older
    assert cache.find(query, limit=1) == (newer,)
    assert cache.find(query, limit=2) == (newer, older)

    cache.close()
    cache.close()
    with pytest.raises(RuntimeError, match="closed"):
        cache.find(query)


def test_cache_query_requires_exact_compatibility_context(
    cache_factory: Callable[[], EvidenceCache],
) -> None:
    cache = cache_factory()
    record = _record()
    cache.store(record)
    incompatible = EvidenceQuery(
        kind=record.kind,
        context={**record.context, "source_convention": "blue-tip-x"},
    )

    assert cache.find(incompatible) == ()
    cache.close()


def test_record_rejects_mutation_nonfinite_data_and_schema_drift() -> None:
    record = _record()
    with pytest.raises(TypeError):
        record.payload["source_id"] = "view-b"  # type: ignore[index]
    assert record.payload["jacobian"]["shape"] == (1, 1)
    with pytest.raises(ValueError, match="does not match"):
        replace(record, payload={**record.payload, "source_id": "view-b"})
    with pytest.raises(ValueError, match="finite JSON-safe"):
        EvidenceRecord.create(
            kind=record.kind,
            context=record.context,
            payload={"gain": float("nan")},
            evidence_refs=record.evidence_refs,
            created_at=record.created_at,
        )
    with pytest.raises(ValueError, match="fields do not match"):
        EvidenceRecord.from_dict({**record.to_dict(), "unexpected": True})


def test_query_bounds_are_enforced(
    cache_factory: Callable[[], EvidenceCache],
) -> None:
    cache = cache_factory()
    query = EvidenceQuery(kind=_record().kind, context=_record().context)
    with pytest.raises(ValueError, match="between 1 and 100"):
        cache.find(query, limit=0)
    with pytest.raises(ValueError, match="between 1 and 100"):
        cache.find(query, limit=101)
    cache.close()


def test_sqlite_cache_can_be_reopened_without_write_authority(tmp_path: Path) -> None:
    path = tmp_path / "evidence.sqlite3"
    writable = SqliteEvidenceCache(path)
    record = _record()
    writable.store(record)
    writable.close()
    original = path.read_bytes()

    read_only = SqliteEvidenceCache(path, read_only=True)
    query = EvidenceQuery(kind=record.kind, context=record.context)
    assert read_only.find(query) == (record,)
    assert read_only.load(record.key) == record
    with pytest.raises(PermissionError, match="read only"):
        read_only.store(record)
    read_only.close()

    assert path.read_bytes() == original


def test_read_only_sqlite_cache_requires_existing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="does not exist"):
        SqliteEvidenceCache(tmp_path / "missing.sqlite3", read_only=True)
