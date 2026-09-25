"""Bounded runtime replacement for nonauthoritative evidence memory."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from entryplug.evidence import (
    EVIDENCE_CACHE_INTERFACE_VERSION,
    EvidenceCache,
    EvidenceQuery,
    EvidenceRecord,
)
from entryplug.operation import AdmissionError, OperationHost

_MAX_TRANSFER_QUERIES = 16
_MAX_RECORDS_PER_QUERY = 100


class ReplacementError(RuntimeError):
    """Typed refusal or failure before a replacement is committed."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class EvidenceCacheView:
    slot: str
    generation: int
    interface_version: str
    implementation: str
    closed: bool


@dataclass(frozen=True, slots=True)
class EvidenceCacheReplacement:
    slot: str
    previous_generation: int
    generation: int
    previous_implementation: str
    implementation: str
    transferred_records: int
    retired_released: bool


def _implementation(value: object) -> str:
    value_type = type(value)
    return f"{value_type.__module__}.{value_type.__qualname__}"


def _release(value: object) -> bool:
    close = getattr(value, "close", None)
    if not callable(close):
        return False
    try:
        close()
    except Exception:
        return False
    return True


def _checked_cache(value: object) -> EvidenceCache:
    if getattr(value, "interface_version", None) != EVIDENCE_CACHE_INTERFACE_VERSION:
        raise ReplacementError(
            "INCOMPATIBLE_INTERFACE",
            f"evidence cache must implement interface {EVIDENCE_CACHE_INTERFACE_VERSION}",
        )
    if getattr(value, "writable", None) is not True:
        raise ReplacementError(
            "READ_ONLY_REPLACEMENT",
            "replacement evidence cache must accept bounded writes",
        )
    for method in ("find", "load", "store", "close"):
        if not callable(getattr(value, method, None)):
            raise ReplacementError(
                "INCOMPATIBLE_INTERFACE",
                f"evidence cache is missing callable {method}",
            )
    return cast(EvidenceCache, value)


class EvidenceCacheSlot:
    """One replaceable cache guarded by the operation host's idle barrier."""

    interface_version = EVIDENCE_CACHE_INTERFACE_VERSION

    def __init__(
        self,
        host: OperationHost,
        cache: EvidenceCache,
        *,
        slot: str = "memory",
    ) -> None:
        if not slot:
            raise ValueError("evidence cache slot must be nonempty")
        self._host = host
        self._cache = _checked_cache(cache)
        self._slot = slot
        self._generation = host.reconfiguration_generation(slot)
        self._closed = False

    @property
    def generation(self) -> int:
        return self._generation

    def observe(self) -> EvidenceCacheView:
        return EvidenceCacheView(
            slot=self._slot,
            generation=self._generation,
            interface_version=self.interface_version,
            implementation=_implementation(self._cache),
            closed=self._closed,
        )

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("evidence cache slot is closed")

    def find(self, query: EvidenceQuery, limit: int = 10) -> tuple[EvidenceRecord, ...]:
        self._require_open()
        return self._cache.find(query, limit)

    def load(self, key: str) -> EvidenceRecord | None:
        self._require_open()
        return self._cache.load(key)

    def store(self, record: EvidenceRecord) -> str:
        self._require_open()
        return self._cache.store(record)

    @staticmethod
    def _transfer(
        source: EvidenceCache,
        target: EvidenceCache,
        queries: tuple[EvidenceQuery, ...],
        transferred: set[str],
    ) -> None:
        for query in queries:
            records = source.find(query, limit=_MAX_RECORDS_PER_QUERY)
            if not isinstance(records, tuple) or len(records) > _MAX_RECORDS_PER_QUERY:
                raise ReplacementError(
                    "TRANSFER_FAILED",
                    "evidence cache returned an invalid bounded query result",
                )
            for record in records:
                if not isinstance(record, EvidenceRecord):
                    raise ReplacementError(
                        "TRANSFER_FAILED",
                        "evidence cache returned an invalid record",
                    )
                if record.key in transferred:
                    if target.load(record.key) != record:
                        raise ReplacementError(
                            "TRANSFER_FAILED",
                            "replacement changed a previously transferred record",
                        )
                    continue
                if target.store(record) != record.key or target.load(record.key) != record:
                    raise ReplacementError(
                        "TRANSFER_FAILED",
                        "replacement did not preserve transferred evidence",
                    )
                transferred.add(record.key)

    @staticmethod
    def _queries(queries: Sequence[EvidenceQuery]) -> tuple[EvidenceQuery, ...]:
        if len(queries) > _MAX_TRANSFER_QUERIES:
            raise ReplacementError(
                "TRANSFER_LIMIT",
                f"at most {_MAX_TRANSFER_QUERIES} evidence queries may be transferred",
            )
        unique: dict[tuple[str, str], EvidenceQuery] = {}
        for query in queries:
            if not isinstance(query, EvidenceQuery):
                raise ReplacementError(
                    "INVALID_ARGUMENT",
                    "transfer queries must be EvidenceQuery values",
                )
            unique.setdefault((query.kind, query.context_json), query)
        return tuple(unique.values())

    def replace(
        self,
        replacement: EvidenceCache,
        *,
        expected_generation: int,
        transfer_queries: Sequence[EvidenceQuery] = (),
    ) -> EvidenceCacheReplacement:
        """Prepare, transfer, and commit one cache generation while idle."""

        if self._closed:
            if replacement is not self._cache:
                _release(replacement)
            raise ReplacementError(
                "SLOT_CLOSED",
                "evidence cache slot is closed",
            )
        if replacement is self._cache:
            raise ReplacementError(
                "INVALID_REPLACEMENT",
                "replacement cache is already active",
            )
        try:
            checked_replacement = _checked_cache(replacement)
        except ReplacementError:
            _release(replacement)
            raise

        committed = False
        try:
            self._require_open()
            if isinstance(expected_generation, bool) or expected_generation != self._generation:
                raise ReplacementError(
                    "STALE_GENERATION",
                    "evidence cache generation is no longer current",
                )
            checked_queries = self._queries(transfer_queries)
            transferred: set[str] = set()

            self._transfer(self._cache, checked_replacement, checked_queries, transferred)
            with self._host.reconfiguration(
                self._slot,
                expected_runtime_id=self._host.runtime_id,
                expected_generation=expected_generation,
            ):
                if expected_generation != self._generation:
                    raise ReplacementError(
                        "STALE_GENERATION",
                        "evidence cache generation changed before commit",
                    )
                self._transfer(self._cache, checked_replacement, checked_queries, transferred)
                retired = self._cache
                previous_generation = self._generation
                previous_implementation = _implementation(retired)
                generation = self._host.commit_reconfiguration(
                    self._slot, expected_generation=expected_generation
                )
                self._cache = checked_replacement
                self._generation = generation
                committed = True
        except (AdmissionError, ReplacementError):
            if not committed:
                _release(checked_replacement)
            raise
        except Exception as error:
            if not committed:
                _release(checked_replacement)
                raise ReplacementError(
                    "TRANSFER_FAILED",
                    "evidence cache preparation or transfer failed",
                ) from error
            raise

        retired_released = _release(retired)
        return EvidenceCacheReplacement(
            slot=self._slot,
            previous_generation=previous_generation,
            generation=self._generation,
            previous_implementation=previous_implementation,
            implementation=_implementation(self._cache),
            transferred_records=len(transferred),
            retired_released=retired_released,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._cache.close()

