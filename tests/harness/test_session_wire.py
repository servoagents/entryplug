"""A local agent client cannot bypass Session admission or invent readiness."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

import pytest

from entryplug.core.evidence import JsonValue
from entryplug.core.operation import (
    AdmissionError,
    CapabilitySpec,
    Lifecycle,
    MotionState,
    OperationContext,
    OperationHost,
    OperationResult,
)
from entryplug.harness.session import Session
from entryplug.harness.session_wire import dispatch_session_request


def test_session_wire_retains_authority_and_request_identity() -> None:
    async def scenario() -> None:
        calls = 0

        def validate(value: Mapping[str, JsonValue]) -> Mapping[str, object]:
            if set(value) != {"target_id"} or value["target_id"] != "marker":
                raise ValueError("unknown target")
            return {"target_id": "marker"}

        async def inspect(
            _: OperationContext, __: Mapping[str, JsonValue]
        ) -> OperationResult:
            nonlocal calls
            calls += 1
            return OperationResult(Lifecycle.SUCCEEDED, MotionState.IDLE, {"seen": True})

        host = OperationHost(
            (
                CapabilitySpec(
                    "inspect_target", "1", "Inspect marker", False, 2.0, 0.1,
                    validate, inspect,
                ),
            ),
            runtime_id="owned-runtime",
        )
        session = Session(host, owns_runtime=True)
        view = await dispatch_session_request(session, {"kind": "observe"})
        assert view["runtime_id"] == "owned-runtime"
        assert view["capabilities"][0]["name"] == "inspect_target"

        request = {
            "kind": "act",
            "capability": "inspect_target",
            "arguments": {"target_id": "marker"},
            "request_id": "stable-request",
            "runtime_id": "owned-runtime",
        }
        with pytest.raises(AdmissionError):
            await dispatch_session_request(session, {**request, "runtime_id": "stale-runtime"})
        with pytest.raises(ValueError, match="unknown Session request"):
            await dispatch_session_request(session, {"kind": "reset"})
        first = await dispatch_session_request(session, request)
        repeated = await dispatch_session_request(session, request)
        assert first["operation_id"] == repeated["operation_id"]
        completed = await dispatch_session_request(
            session,
            {"kind": "wait", "operation_id": first["operation_id"], "timeout_seconds": 1.0},
        )
        assert completed["lifecycle"] == "succeeded"
        assert completed["result"] == {"seen": True}
        assert calls == 1
        with pytest.raises(ValueError, match="finite"):
            await dispatch_session_request(
                session,
                {"kind": "wait", "operation_id": first["operation_id"], "timeout_seconds": 8},
            )
        await session.close()

    asyncio.run(scenario())
