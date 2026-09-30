"""Scoped Session projection for existing protocol exports; never owns a body."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from entryplug.core.evidence import JsonValue
from entryplug.core.operation import (
    AdmissionError,
    EffectState,
    Lifecycle,
    MotionState,
    OperationSnapshot,
    RuntimeView,
)
from entryplug.harness.session import Session
from entryplug_app.contracts import AppError
from entryplug_app.service import ApplicationService, new_id


def operation_snapshot(op: dict[str, Any]) -> OperationSnapshot:
    return OperationSnapshot(
        operation_id=op["id"],
        runtime_id=op["session_runtime_id"],
        request_id=op["request_id"],
        capability=op["capability"],
        lifecycle=Lifecycle(op["lifecycle"]),
        motion_state=MotionState(op.get("motion_state", "idle")),
        phase=op.get("phase", op["dispatch"]),
        accepted_monotonic=0,
        deadline_monotonic=0,
        completed_monotonic=None,
        cancel_requested=op["cancel_requested"],
        reason_code=op.get("reason_code"),
        result=op.get("result", {}),
        effect_state=EffectState(op.get("effect_state", "none")),
    )


class BorrowedSession(Session):
    def __init__(self, service: ApplicationService, attachment_id: str):
        # All verbs delegate through the application; there is no second OperationHost.
        self.service, self.attachment_id = service, attachment_id

    @property
    def owns_runtime(self) -> bool:
        return False

    async def _attachment(self) -> dict[str, Any]:
        try:
            attachment = await self.service.store.get("attachments", self.attachment_id)
            if attachment["status"] != "active":
                raise AppError("access_revoked", "External body access was revoked", 403)
            return attachment
        except AppError as error:
            raise AdmissionError(error.code, str(error)) from error

    async def observe(self) -> RuntimeView:
        attachment = await self._attachment()
        port = self.service.ports.get(attachment["body_id"])
        if not port:
            raise AdmissionError("SOURCE_LOST", "The attached body is unavailable")
        view = await port.session.observe()
        operations = tuple(
            operation_snapshot(op)
            for op in await self.service.store.list("operations")
            if op.get("attachment_id") == self.attachment_id
        )
        return replace(
            view,
            capabilities=tuple(c for c in view.capabilities if c["name"] in attachment["allow"]),
            operations=operations,
        )

    async def act(
        self,
        capability: str,
        arguments: Mapping[str, object],
        *,
        request_id: str | None = None,
        expected_runtime_id: str | None = None,
    ) -> OperationSnapshot:
        view = await self.observe()
        if expected_runtime_id is not None and expected_runtime_id != view.runtime_id:
            raise AdmissionError("STALE_RUNTIME", "Runtime ID is no longer current")
        try:
            op = await self.service.command(
                "operation.create",
                {
                    "attachment_id": self.attachment_id,
                    "capability": capability,
                    "arguments": dict(arguments),
                },
                request_id or new_id("request"),
                actor=self.attachment_id,
            )
            return operation_snapshot(op)
        except AppError as error:
            raise AdmissionError(error.code, str(error)) from error

    async def inspect(
        self, reference: str | OperationSnapshot, detail: str = "summary"
    ) -> Mapping[str, JsonValue]:
        await self._attachment()
        if detail not in {"summary", "result"}:
            raise ValueError("detail must be summary or result")
        try:
            op = await self.service.store.get("operations", self._reference(reference))
            await self.service.authorize_operation(op, self.attachment_id)
            result = {
                "operation_id": op["id"],
                "native_operation_id": op.get("native_operation_id"),
                "lifecycle": op["lifecycle"],
                "capability": op["capability"],
                "motion_state": op.get("motion_state", "idle"),
                "effect_state": op.get("effect_state", "none"),
                "reason_code": op.get("reason_code"),
                "phase": op.get("phase", op["dispatch"]),
                "cancel_requested": op["cancel_requested"],
                "evidence_ids": op["evidence_ids"],
            }
            if detail == "result":
                result["result"] = op.get("result", {})
            return result
        except AppError as error:
            raise AdmissionError(error.code, str(error)) from error

    async def wait(self, operation: str | OperationSnapshot, timeout_s: float) -> OperationSnapshot:
        deadline = time.monotonic() + min(max(timeout_s, 0), 5)
        identifier = self._reference(operation)
        while True:
            await self.inspect(identifier)
            op = operation_snapshot(await self.service.store.get("operations", identifier))
            if op.terminal or time.monotonic() >= deadline:
                return op
            await asyncio.sleep(0.02)

    async def cancel(self, operation: str | OperationSnapshot) -> OperationSnapshot:
        await self._attachment()
        try:
            op = await self.service.command(
                "operation.cancel",
                {"operation_id": self._reference(operation)},
                new_id("cancel"),
                actor=self.attachment_id,
            )
            return operation_snapshot(op)
        except AppError as error:
            raise AdmissionError(error.code, str(error)) from error

    async def close(self) -> tuple[OperationSnapshot, ...]:
        return ()

    async def __aenter__(self) -> BorrowedSession:
        await self._attachment()
        return self
