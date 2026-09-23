"""Compact agent-facing session over the shared operation host."""

from __future__ import annotations

import uuid
from collections.abc import Mapping

from entryplug.evidence import JsonValue
from entryplug.operation import OperationHost, OperationSnapshot, RuntimeView


class Session:
    """Expose observe, act, wait, cancel, and inspect without owning task policy."""

    def __init__(self, host: OperationHost, *, owns_runtime: bool = False) -> None:
        self._host = host
        self._owns_runtime = owns_runtime
        self._closed = False

    async def observe(self) -> RuntimeView:
        self._require_open()
        return self._host.observe()

    async def act(
        self,
        capability: str,
        arguments: Mapping[str, object],
        *,
        request_id: str | None = None,
    ) -> OperationSnapshot:
        self._require_open()
        return await self._host.start(
            capability,
            arguments,
            request_id=request_id or uuid.uuid4().hex,
            expected_runtime_id=self._host.runtime_id,
        )

    async def wait(
        self, operation: str | OperationSnapshot, timeout_s: float
    ) -> OperationSnapshot:
        self._require_open()
        return await self._host.wait(self._reference(operation), timeout_s)

    async def cancel(self, operation: str | OperationSnapshot) -> OperationSnapshot:
        self._require_open()
        return await self._host.cancel(self._reference(operation))

    async def inspect(
        self, reference: str | OperationSnapshot, detail: str = "summary"
    ) -> Mapping[str, JsonValue]:
        self._require_open()
        return await self._host.inspect(self._reference(reference), detail)

    async def close(self) -> tuple[OperationSnapshot, ...]:
        if self._closed:
            return ()
        self._closed = True
        if self._owns_runtime:
            return await self._host.close()
        return ()

    async def __aenter__(self) -> "Session":
        self._require_open()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("session is closed")

    @staticmethod
    def _reference(value: str | OperationSnapshot) -> str:
        return value if isinstance(value, str) else value.operation_id
