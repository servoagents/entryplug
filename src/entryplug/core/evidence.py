"""Small immutable evidence caches with one JSON-safe record format."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Protocol, cast

EVIDENCE_CACHE_INTERFACE_VERSION = "1"

JsonScalar = None | bool | int | float | str
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _freeze(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _canonical(value: object, label: str) -> str:
    try:
        return json.dumps(
            _plain(value),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must be finite JSON-safe data") from error


def json_object(value: Mapping[str, object], label: str) -> dict[str, JsonValue]:
    """Return a detached JSON object, normalizing immutable runtime views."""

    encoded = _canonical(dict(value), label)
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):  # defensive: Mapping should always encode as object
        raise ValueError(f"{label} must be a JSON object")
    return decoded


@dataclass(frozen=True, slots=True)
class EvidenceQuery:
    """Exact compatibility query for immutable candidate evidence."""

    kind: str
    context: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        if not self.kind:
            raise ValueError("evidence kind must be nonempty")
        checked = json_object(self.context, "query context")
        object.__setattr__(self, "context", _freeze(checked))

    @property
    def context_json(self) -> str:
        return _canonical(self.context, "query context")


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    """Content-addressed evidence candidate; never operation authority."""

    key: str
    kind: str
    context: Mapping[str, JsonValue]
    payload: Mapping[str, JsonValue]
    evidence_refs: tuple[str, ...]
    created_at: str

    def __post_init__(self) -> None:
        if not self.kind or not self.created_at:
            raise ValueError("evidence kind and creation time must be nonempty")
        if not self.evidence_refs or not all(
            isinstance(item, str) and item for item in self.evidence_refs
        ):
            raise ValueError("evidence references must be nonempty")
        checked_context = json_object(self.context, "context")
        checked_payload = json_object(self.payload, "payload")
        object.__setattr__(self, "context", _freeze(checked_context))
        object.__setattr__(self, "payload", _freeze(checked_payload))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        expected = self.content_key(
            kind=self.kind,
            context=self.context,
            payload=self.payload,
            evidence_refs=self.evidence_refs,
            created_at=self.created_at,
        )
        if self.key != expected:
            raise ValueError("evidence key does not match record content")

    @staticmethod
    def content_key(
        *,
        kind: str,
        context: Mapping[str, JsonValue],
        payload: Mapping[str, JsonValue],
        evidence_refs: Sequence[str],
        created_at: str,
    ) -> str:
        body = {
            "kind": kind,
            "context": dict(context),
            "payload": dict(payload),
            "evidence_refs": list(evidence_refs),
            "created_at": created_at,
        }
        digest = hashlib.sha256(_canonical(body, "evidence record").encode()).hexdigest()
        return f"sha256:{digest}"

    @classmethod
    def create(
        cls,
        *,
        kind: str,
        context: Mapping[str, JsonValue],
        payload: Mapping[str, JsonValue],
        evidence_refs: Sequence[str],
        created_at: str,
    ) -> EvidenceRecord:
        refs = tuple(evidence_refs)
        checked_context = json_object(context, "context")
        checked_payload = json_object(payload, "payload")
        key = cls.content_key(
            kind=kind,
            context=checked_context,
            payload=checked_payload,
            evidence_refs=refs,
            created_at=created_at,
        )
        return cls(
            key=key,
            kind=kind,
            context=checked_context,
            payload=checked_payload,
            evidence_refs=refs,
            created_at=created_at,
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "key": self.key,
            "kind": self.kind,
            "context": cast(JsonValue, _plain(self.context)),
            "payload": cast(JsonValue, _plain(self.payload)),
            "evidence_refs": list(self.evidence_refs),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> EvidenceRecord:
        expected = {
            "key",
            "kind",
            "context",
            "payload",
            "evidence_refs",
            "created_at",
        }
        if set(value) != expected:
            raise ValueError("evidence record fields do not match schema")
        context = value["context"]
        payload = value["payload"]
        refs = value["evidence_refs"]
        if not isinstance(context, dict) or not isinstance(payload, dict):
            raise ValueError("evidence context and payload must be JSON objects")
        if not isinstance(refs, list) or not all(isinstance(item, str) for item in refs):
            raise ValueError("evidence references must be a string list")
        key = value["key"]
        kind = value["kind"]
        created_at = value["created_at"]
        if not isinstance(key, str) or not isinstance(kind, str) or not isinstance(created_at, str):
            raise ValueError("evidence key, kind, and creation time must be strings")
        checked_refs = tuple(cast(str, item) for item in refs)
        return cls(
            key=key,
            kind=kind,
            context=context,
            payload=payload,
            evidence_refs=checked_refs,
            created_at=created_at,
        )


class EvidenceCache(Protocol):
    """Replaceable candidate memory; a cache hit never grants readiness."""

    interface_version: str
    writable: bool

    def find(self, query: EvidenceQuery, limit: int = 10) -> tuple[EvidenceRecord, ...]: ...

    def load(self, key: str) -> EvidenceRecord | None: ...

    def store(self, record: EvidenceRecord) -> str: ...

    def close(self) -> None: ...


def _limit(value: int) -> int:
    if isinstance(value, bool) or value < 1 or value > 100:
        raise ValueError("evidence query limit must be between 1 and 100")
    return value


class MemoryEvidenceCache:
    """Process-local evidence candidate cache with deterministic ordering."""

    interface_version = EVIDENCE_CACHE_INTERFACE_VERSION
    writable = True

    def __init__(self) -> None:
        self._records: dict[str, EvidenceRecord] = {}
        self._closed = False

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("evidence cache is closed")

    def find(self, query: EvidenceQuery, limit: int = 10) -> tuple[EvidenceRecord, ...]:
        self._require_open()
        checked_limit = _limit(limit)
        records = (
            item
            for item in self._records.values()
            if item.kind == query.kind and item.context == query.context
        )
        ordered = sorted(records, key=lambda item: (item.created_at, item.key), reverse=True)
        return tuple(ordered[:checked_limit])

    def load(self, key: str) -> EvidenceRecord | None:
        self._require_open()
        return self._records.get(key)

    def store(self, record: EvidenceRecord) -> str:
        self._require_open()
        self._records.setdefault(record.key, record)
        return record.key

    def close(self) -> None:
        self._closed = True


class SqliteEvidenceCache:
    """SQLite implementation of the same bounded immutable cache contract."""

    interface_version = EVIDENCE_CACHE_INTERFACE_VERSION

    def __init__(self, path: Path, *, read_only: bool = False) -> None:
        self._read_only = read_only
        self.writable = not read_only
        self._connection: sqlite3.Connection | None
        if read_only:
            if not path.is_file():
                raise FileNotFoundError(f"evidence cache does not exist: {path}")
            uri = f"{path.resolve().as_uri()}?mode=ro&immutable=1"
            self._connection = sqlite3.connect(uri, uri=True)
            return

        self._connection = sqlite3.connect(path)
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS evidence_records (
                key TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                context_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                record_json TEXT NOT NULL
            )
            """
        )
        self._connection.execute(
            """
            CREATE INDEX IF NOT EXISTS evidence_records_lookup
            ON evidence_records(kind, context_json, created_at DESC)
            """
        )
        self._connection.commit()

    def _open(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("evidence cache is closed")
        return self._connection

    @staticmethod
    def _decode(encoded: str) -> EvidenceRecord:
        value = json.loads(encoded)
        if not isinstance(value, dict):
            raise ValueError("stored evidence record must be a JSON object")
        return EvidenceRecord.from_dict(value)

    def find(self, query: EvidenceQuery, limit: int = 10) -> tuple[EvidenceRecord, ...]:
        rows = self._open().execute(
            """
            SELECT record_json FROM evidence_records
            WHERE kind = ? AND context_json = ?
            ORDER BY created_at DESC, key DESC
            LIMIT ?
            """,
            (query.kind, query.context_json, _limit(limit)),
        )
        return tuple(self._decode(row[0]) for row in rows)

    def load(self, key: str) -> EvidenceRecord | None:
        row = (
            self._open()
            .execute("SELECT record_json FROM evidence_records WHERE key = ?", (key,))
            .fetchone()
        )
        return None if row is None else self._decode(row[0])

    def store(self, record: EvidenceRecord) -> str:
        if self._read_only:
            raise PermissionError("evidence cache is read only")
        encoded = _canonical(record.to_dict(), "evidence record")
        with self._open():
            self._open().execute(
                """
                INSERT OR IGNORE INTO evidence_records
                    (key, kind, context_json, created_at, record_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    record.key,
                    record.kind,
                    _canonical(record.context, "context"),
                    record.created_at,
                    encoded,
                ),
            )
        return record.key

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
