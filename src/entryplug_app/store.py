"""SQLite WAL journal, confined to one owned disk thread.

Application state is owned by asyncio; only SQL and disk IO run on this worker.
Each write atomically commits records, deduplication and its durable event.
"""

from __future__ import annotations

import asyncio
import builtins
import json
import sqlite3
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, TypeVar, cast

from entryplug_app.contracts import AppError, canonical, utc_now

ResultT = TypeVar("ResultT")


class Store:
    def __init__(self, path: Path, *, event_retention: int = 10_000, max_bytes: int = 268_435_456):
        self.path, self.retention, self.max_bytes = path, event_retention, max_bytes
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="entryplug-store")
        self._db: sqlite3.Connection | None = None
        self.workspace_id = ""
        self.instance_id = "boot_" + uuid.uuid4().hex

    async def _call(self, method: Callable[..., ResultT], *args: Any) -> ResultT:
        return await asyncio.get_running_loop().run_in_executor(self._worker, method, *args)

    async def open(self) -> None:
        self.workspace_id = await self._call(self._open)

    def _open(self) -> str:
        self._db = sqlite3.connect(self.path)
        self.path.chmod(0o600)
        db = self._db
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version not in {0, 1}:
            raise AppError("migration_required", "Unsupported workspace database version", 409)
        with db:
            if version == 0:
                db.executescript("""
                    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE records (kind TEXT, id TEXT, document TEXT NOT NULL,
                                          PRIMARY KEY(kind,id));
                    CREATE TABLE requests (scope TEXT, key TEXT, hash TEXT NOT NULL,
                                           response TEXT NOT NULL, PRIMARY KEY(scope,key));
                    CREATE TABLE events (seq INTEGER PRIMARY KEY AUTOINCREMENT,
                                         document TEXT NOT NULL);
                    PRAGMA user_version=1;
                """)
                db.execute(
                    "INSERT INTO meta VALUES ('workspace_id', ?)", ("ws_" + uuid.uuid4().hex,)
                )
            return str(db.execute("SELECT value FROM meta WHERE key='workspace_id'").fetchone()[0])

    async def get(self, kind: str, identifier: str) -> dict[str, Any]:
        result = await self._call(self._get, kind, identifier)
        if result is None:
            raise AppError("not_found", f"{kind} was not found", 404)
        return result

    def _get(self, kind: str, identifier: str) -> dict[str, Any] | None:
        assert self._db
        row = self._db.execute(
            "SELECT document FROM records WHERE kind=? AND id=?", (kind, identifier)
        ).fetchone()
        return cast(dict[str, Any], json.loads(row[0])) if row else None

    async def list(self, kind: str) -> builtins.list[dict[str, Any]]:
        return await self._call(self._list, kind)

    def _list(self, kind: str) -> builtins.list[dict[str, Any]]:
        assert self._db
        return [
            json.loads(row[0])
            for row in self._db.execute(
                "SELECT document FROM records WHERE kind=? ORDER BY rowid", (kind,)
            )
        ]

    async def request(self, scope: str, key: str, fingerprint: str) -> dict[str, Any] | None:
        return await self._call(self._request, scope, key, fingerprint)

    def _request(self, scope: str, key: str, fingerprint: str) -> dict[str, Any] | None:
        assert self._db
        row = self._db.execute(
            "SELECT hash,response FROM requests WHERE scope=? AND key=?", (scope, key)
        ).fetchone()
        if row and row[0] != fingerprint:
            raise AppError("idempotency_conflict", "This key was used for different content", 409)
        return cast(dict[str, Any], json.loads(row[1])) if row else None

    async def commit(
        self,
        records: builtins.list[tuple[str, str, dict[str, Any]]],
        event_type: str,
        data: dict[str, Any],
        *,
        run_id: str | None = None,
        turn_id: str | None = None,
        operation_id: str | None = None,
        request: tuple[str, str, str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        event = {
            "schema_version": 1,
            "workspace_id": self.workspace_id,
            "service_instance_id": self.instance_id,
            "at": utc_now(),
            "type": event_type,
            "run_id": run_id,
            "turn_id": turn_id,
            "operation_id": operation_id,
            "data": data,
        }
        return await self._call(self._commit, records, event, request)

    def _commit(self, records: Any, event: Any, request: Any) -> dict[str, Any]:
        assert self._db
        db = self._db
        with db:
            for kind, identifier, document in records:
                db.execute(
                    "INSERT INTO records VALUES (?,?,?) ON CONFLICT(kind,id) "
                    "DO UPDATE SET document=excluded.document",
                    (kind, identifier, canonical(document)),
                )
            if request:
                scope, key, fingerprint, response = request
                db.execute(
                    "INSERT INTO requests VALUES (?,?,?,?)",
                    (scope, key, fingerprint, canonical(response)),
                )
            cursor = db.execute("INSERT INTO events(document) VALUES (?)", (canonical(event),))
            event["seq"] = cursor.lastrowid
            db.execute("UPDATE events SET document=? WHERE seq=?", (canonical(event), event["seq"]))
            db.execute("DELETE FROM events WHERE seq <= ?", (event["seq"] - self.retention,))
        return cast(dict[str, Any], event)

    async def snapshot(self) -> dict[str, Any]:
        return await self._call(self._snapshot)

    def _snapshot(self) -> dict[str, Any]:
        assert self._db
        with self._db:
            result = {
                kind: self._list(kind)
                for kind in (
                    "missions",
                    "runs",
                    "turns",
                    "operations",
                    "alerts",
                    "connections",
                    "attachments",
                    "approvals",
                )
            }
            result["cursor"] = self._db.execute(
                "SELECT COALESCE(MAX(seq),0) FROM events"
            ).fetchone()[0]
        return {
            **result,
            "workspace_id": self.workspace_id,
            "service_instance_id": self.instance_id,
        }

    async def events(
        self, after: int, run_id: str | None = None, limit: int = 128
    ) -> builtins.list[dict[str, Any]]:
        return await self._call(self._events, after, run_id, limit)

    def _events(self, after: int, run_id: str | None, limit: int) -> builtins.list[dict[str, Any]]:
        assert self._db
        minimum, maximum = self._db.execute("SELECT MIN(seq), MAX(seq) FROM events").fetchone()
        if minimum is not None and after < minimum - 1:
            raise AppError("replay_expired", "Refresh the snapshot before reconnecting", 410)
        if after < 0 or (maximum is not None and after > maximum):
            raise AppError("invalid_cursor", "Cursor is outside this workspace history", 400)
        query = "SELECT document FROM events WHERE seq > ?"
        args: builtins.list[Any] = [after]
        if run_id:
            query += " AND json_extract(document, '$.run_id')=?"
            args.append(run_id)
        return [
            json.loads(row[0])
            for row in self._db.execute(query + " ORDER BY seq LIMIT ?", (*args, limit))
        ]

    async def check_capacity(self) -> None:
        size = await self._call(
            lambda: sum(
                p.stat().st_size for p in self.path.parent.glob(self.path.name + "*") if p.is_file()
            )
        )
        if size >= self.max_bytes:
            raise AppError(
                "storage_limit",
                "Workspace storage budget reached; export/archive before new work",
                507,
            )

    async def close(self) -> None:
        if self._db:
            await self._call(self._db.close)
            self._db = None
        self._worker.shutdown(wait=True)
