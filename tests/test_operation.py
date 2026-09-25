from __future__ import annotations

import asyncio
from collections.abc import Mapping

import pytest

from entryplug.evidence import JsonValue
from entryplug.operation import (
    AdmissionError,
    CapabilitySpec,
    Lifecycle,
    MotionState,
    OperationContext,
    OperationHost,
    OperationResult,
)
from entryplug.session import Session


def _arguments(value: Mapping[str, JsonValue]) -> Mapping[str, object]:
    if set(value) != {"value"} or not isinstance(value["value"], int):
        raise ValueError("value must be one integer")
    return {"value": value["value"]}


def _run(scenario) -> None:
    asyncio.run(scenario())


def test_session_exposes_five_verbs_over_one_operation_host() -> None:
    async def scenario() -> None:
        async def read(
            context: OperationContext, arguments: Mapping[str, JsonValue]
        ) -> OperationResult:
            context.report("read", MotionState.IDLE)
            return OperationResult(
                Lifecycle.SUCCEEDED,
                MotionState.IDLE,
                {"observed": arguments["value"]},
            )

        host = OperationHost(
            (
                CapabilitySpec(
                    "read_value",
                    "1",
                    "Read one fixture value",
                    False,
                    1.0,
                    0.1,
                    _arguments,
                    read,
                ),
            ),
            runtime_id="runtime-a",
        )
        session = Session(host, owns_runtime=True)

        view = await session.observe()
        assert view.runtime_id == "runtime-a"
        assert view.capabilities[0]["name"] == "read_value"
        operation = await session.act("read_value", {"value": 7})
        completed = await session.wait(operation, timeout_s=0.2)
        inspected = await session.inspect(completed, detail="result")

        assert completed.lifecycle == Lifecycle.SUCCEEDED
        assert inspected["result"] == {"observed": 7}
        assert (await session.cancel(completed)).lifecycle == Lifecycle.SUCCEEDED
        await session.close()
        with pytest.raises(RuntimeError, match="closed"):
            await session.observe()

    _run(scenario)


def test_duplicate_request_returns_one_operation_and_changed_input_conflicts() -> None:
    async def scenario() -> None:
        release = asyncio.Event()
        calls = 0

        async def move(
            context: OperationContext, arguments: Mapping[str, JsonValue]
        ) -> OperationResult:
            nonlocal calls
            calls += 1
            context.report("execute", MotionState.MOVING)
            await release.wait()
            return OperationResult(
                Lifecycle.SUCCEEDED,
                MotionState.HOLDING,
                {"applied": arguments["value"]},
            )

        host = OperationHost(
            (
                CapabilitySpec(
                    "move",
                    "1",
                    "Move the fixture",
                    True,
                    1.0,
                    0.1,
                    _arguments,
                    move,
                ),
            ),
            runtime_id="runtime-a",
        )
        first = await host.start(
            "move", {"value": 1}, request_id="same", expected_runtime_id="runtime-a"
        )
        repeated = await host.start(
            "move", {"value": 1}, request_id="same", expected_runtime_id="runtime-a"
        )
        assert repeated.operation_id == first.operation_id
        with pytest.raises(AdmissionError, match="different") as conflict:
            await host.start(
                "move",
                {"value": 2},
                request_id="same",
                expected_runtime_id="runtime-a",
            )
        assert conflict.value.reason_code == "REQUEST_CONFLICT"

        await asyncio.sleep(0)
        assert calls == 1
        release.set()
        assert (await host.wait(first.operation_id, 0.2)).lifecycle == Lifecycle.SUCCEEDED
        await host.close()

    _run(scenario)


def test_motion_is_single_owner_while_read_only_work_remains_available() -> None:
    async def scenario() -> None:
        release = asyncio.Event()

        async def move(context: OperationContext, _: Mapping[str, JsonValue]) -> OperationResult:
            context.report("execute", MotionState.MOVING)
            await release.wait()
            return OperationResult(Lifecycle.SUCCEEDED, MotionState.HOLDING)

        async def read(_: OperationContext, arguments: Mapping[str, JsonValue]) -> OperationResult:
            return OperationResult(
                Lifecycle.SUCCEEDED, MotionState.IDLE, {"value": arguments["value"]}
            )

        specs = (
            CapabilitySpec("move", "1", "Move", True, 1.0, 0.1, _arguments, move),
            CapabilitySpec("read", "1", "Read", False, 1.0, 0.1, _arguments, read),
        )
        host = OperationHost(specs, runtime_id="runtime-a")
        active = await host.start(
            "move", {"value": 1}, request_id="move-1", expected_runtime_id="runtime-a"
        )
        with pytest.raises(AdmissionError) as busy:
            await host.start(
                "move",
                {"value": 2},
                request_id="move-2",
                expected_runtime_id="runtime-a",
            )
        assert busy.value.reason_code == "BUSY"
        read_operation = await host.start(
            "read", {"value": 3}, request_id="read-1", expected_runtime_id="runtime-a"
        )
        assert (await host.wait(read_operation.operation_id, 0.2)).lifecycle == Lifecycle.SUCCEEDED
        release.set()
        await host.wait(active.operation_id, 0.2)
        await host.close()

    _run(scenario)


def test_wait_timeout_does_not_cancel_and_cancel_requires_handler_confirmation() -> None:
    async def scenario() -> None:
        async def cancellable(
            context: OperationContext, _: Mapping[str, JsonValue]
        ) -> OperationResult:
            context.report("execute", MotionState.MOVING)
            await context.wait_for_cancel()
            context.report("stop", MotionState.HOLDING)
            return OperationResult(Lifecycle.CANCELED, MotionState.HOLDING)

        host = OperationHost(
            (CapabilitySpec("move", "1", "Move", True, 1.0, 0.2, _arguments, cancellable),),
            runtime_id="runtime-a",
        )
        operation = await host.start(
            "move", {"value": 1}, request_id="move-1", expected_runtime_id="runtime-a"
        )
        timed_out = await host.wait(operation.operation_id, 0.0)
        assert timed_out.lifecycle in {Lifecycle.ACCEPTED, Lifecycle.RUNNING}
        assert not timed_out.cancel_requested

        canceling = await host.cancel(operation.operation_id)
        assert canceling.lifecycle == Lifecycle.CANCELING
        assert canceling.motion_state in {MotionState.IDLE, MotionState.MOVING}
        canceled = await host.wait(operation.operation_id, 0.2)
        assert canceled.lifecycle == Lifecycle.CANCELED
        assert canceled.motion_state == MotionState.HOLDING
        await host.close()

    _run(scenario)


def test_cancel_before_worker_start_is_not_overwritten_by_running_state() -> None:
    async def scenario() -> None:
        async def cancellable(
            context: OperationContext, _: Mapping[str, JsonValue]
        ) -> OperationResult:
            await context.wait_for_cancel()
            return OperationResult(Lifecycle.CANCELED, MotionState.HOLDING)

        host = OperationHost(
            (CapabilitySpec("move", "1", "Move", True, 1.0, 0.2, _arguments, cancellable),),
            runtime_id="runtime-a",
        )
        operation = await host.start(
            "move", {"value": 1}, request_id="move-1", expected_runtime_id="runtime-a"
        )
        assert (await host.cancel(operation.operation_id)).lifecycle == Lifecycle.CANCELING
        await asyncio.sleep(0)
        current = await host.wait(operation.operation_id, 0.2)
        assert current.lifecycle == Lifecycle.CANCELED
        assert current.motion_state == MotionState.HOLDING
        await host.close()

    _run(scenario)


def test_rejected_validation_is_deduplicated_without_revalidating() -> None:
    async def scenario() -> None:
        validations = 0

        def reject(_: Mapping[str, JsonValue]) -> Mapping[str, object]:
            nonlocal validations
            validations += 1
            raise ValueError("fixture rejected the value")

        async def unused(_: OperationContext, __: Mapping[str, JsonValue]) -> OperationResult:
            raise AssertionError("rejected work must not run")

        host = OperationHost(
            (CapabilitySpec("move", "1", "Move", True, 1.0, 0.1, reject, unused),),
            runtime_id="runtime-a",
        )
        for _ in range(2):
            with pytest.raises(AdmissionError) as rejected:
                await host.start(
                    "move",
                    {"value": 1},
                    request_id="invalid",
                    expected_runtime_id="runtime-a",
                )
            assert rejected.value.reason_code == "INVALID_ARGUMENT"
        assert validations == 1
        assert host.observe().request_records == 1
        await host.close()

    _run(scenario)


def test_deadline_with_confirmed_stop_is_failed_not_canceled() -> None:
    async def scenario() -> None:
        async def deadline_stop(
            context: OperationContext, _: Mapping[str, JsonValue]
        ) -> OperationResult:
            context.report("execute", MotionState.MOVING)
            await context.wait_for_cancel()
            return OperationResult(Lifecycle.CANCELED, MotionState.HOLDING)

        host = OperationHost(
            (CapabilitySpec("move", "1", "Move", True, 0.02, 0.1, _arguments, deadline_stop),),
            runtime_id="runtime-a",
        )
        operation = await host.start(
            "move", {"value": 1}, request_id="move-1", expected_runtime_id="runtime-a"
        )
        result = await host.wait(operation.operation_id, 0.2)
        assert result.lifecycle == Lifecycle.FAILED
        assert result.motion_state == MotionState.HOLDING
        assert result.reason_code == "DEADLINE_EXCEEDED"
        await host.close()

    _run(scenario)


def test_stale_runtime_and_request_limit_reject_before_effects_but_cancel_survives() -> None:
    async def scenario() -> None:
        calls = 0

        async def cancellable(
            context: OperationContext, _: Mapping[str, JsonValue]
        ) -> OperationResult:
            nonlocal calls
            calls += 1
            await context.wait_for_cancel()
            return OperationResult(Lifecycle.CANCELED, MotionState.HOLDING)

        host = OperationHost(
            (CapabilitySpec("move", "1", "Move", True, 1.0, 0.2, _arguments, cancellable),),
            runtime_id="runtime-new",
            maximum_requests=2,
        )
        with pytest.raises(AdmissionError) as stale:
            await host.start(
                "move", {"value": 1}, request_id="old", expected_runtime_id="runtime-old"
            )
        assert stale.value.reason_code == "STALE_RUNTIME"
        active = await host.start(
            "move", {"value": 1}, request_id="new", expected_runtime_id="runtime-new"
        )
        with pytest.raises(AdmissionError) as full:
            await host.start(
                "move", {"value": 2}, request_id="extra", expected_runtime_id="runtime-new"
            )
        assert full.value.reason_code == "REQUEST_LIMIT"
        assert calls <= 1
        await host.cancel(active.operation_id)
        assert (await host.wait(active.operation_id, 0.2)).lifecycle == Lifecycle.CANCELED
        await host.close()

    _run(scenario)


def test_unconfirmed_deadline_inhibits_later_motion_without_blocking_reads() -> None:
    async def scenario() -> None:
        async def hung(context: OperationContext, _: Mapping[str, JsonValue]) -> OperationResult:
            context.report("execute", MotionState.MOVING)
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def read(_: OperationContext, arguments: Mapping[str, JsonValue]) -> OperationResult:
            return OperationResult(
                Lifecycle.SUCCEEDED, MotionState.IDLE, {"value": arguments["value"]}
            )

        host = OperationHost(
            (
                CapabilitySpec("move", "1", "Move", True, 0.02, 0.02, _arguments, hung),
                CapabilitySpec("read", "1", "Read", False, 1.0, 0.1, _arguments, read),
            ),
            runtime_id="runtime-a",
        )
        operation = await host.start(
            "move", {"value": 1}, request_id="move-1", expected_runtime_id="runtime-a"
        )
        result = await host.wait(operation.operation_id, 0.2)
        assert result.lifecycle == Lifecycle.INDETERMINATE
        assert result.motion_state == MotionState.UNKNOWN
        assert host.observe().motion_inhibited_reason == "STOP_UNCONFIRMED"
        with pytest.raises(AdmissionError) as inhibited:
            await host.start(
                "move", {"value": 2}, request_id="move-2", expected_runtime_id="runtime-a"
            )
        assert inhibited.value.reason_code == "MOTION_INHIBITED"
        read_operation = await host.start(
            "read", {"value": 3}, request_id="read-1", expected_runtime_id="runtime-a"
        )
        assert (await host.wait(read_operation.operation_id, 0.2)).lifecycle == Lifecycle.SUCCEEDED
        await host.close()

    _run(scenario)


def test_detached_session_does_not_cancel_runtime_owned_work() -> None:
    async def scenario() -> None:
        release = asyncio.Event()

        async def move(_: OperationContext, __: Mapping[str, JsonValue]) -> OperationResult:
            await release.wait()
            return OperationResult(Lifecycle.SUCCEEDED, MotionState.HOLDING)

        host = OperationHost(
            (CapabilitySpec("move", "1", "Move", True, 1.0, 0.1, _arguments, move),),
            runtime_id="runtime-a",
        )
        session = Session(host, owns_runtime=False)
        operation = await session.act("move", {"value": 1})
        await session.close()
        assert host.observe().active_motion_operation == operation.operation_id
        release.set()
        assert (await host.wait(operation.operation_id, 0.2)).lifecycle == Lifecycle.SUCCEEDED
        await host.close()

    _run(scenario)


def test_capability_schemas_are_validated_frozen_and_observable() -> None:
    async def unused(_: OperationContext, __: Mapping[str, JsonValue]) -> OperationResult:
        return OperationResult(Lifecycle.SUCCEEDED, MotionState.IDLE)

    input_schema = {
        "type": "object",
        "properties": {"value": {"type": "integer"}},
        "required": ["value"],
        "additionalProperties": False,
    }
    host = OperationHost(
        (
            CapabilitySpec(
                "read_value",
                "1",
                "Read one value",
                False,
                1.0,
                0.1,
                _arguments,
                unused,
                input_schema,
                {"type": "object", "additionalProperties": True},
            ),
        )
    )
    input_schema["type"] = "array"

    published = host.observe().capabilities[0]
    assert published["input_schema"] == {
        "type": "object",
        "properties": {"value": {"type": "integer"}},
        "required": ("value",),
        "additionalProperties": False,
    }
    schema = published["input_schema"]
    assert isinstance(schema, Mapping)
    with pytest.raises(TypeError):
        schema["type"] = "array"  # type: ignore[index]

    with pytest.raises(ValueError, match="root type must be object"):
        CapabilitySpec(
            "invalid",
            "1",
            "Invalid schema",
            False,
            1.0,
            0.1,
            _arguments,
            unused,
            {"type": "array"},
        )


def test_session_can_reject_a_stale_external_runtime_id() -> None:
    async def scenario() -> None:
        async def read(_: OperationContext, arguments: Mapping[str, JsonValue]) -> OperationResult:
            return OperationResult(Lifecycle.SUCCEEDED, MotionState.IDLE, arguments)

        session = Session(
            OperationHost(
                (
                    CapabilitySpec(
                        "read",
                        "1",
                        "Read",
                        False,
                        1.0,
                        0.1,
                        _arguments,
                        read,
                    ),
                ),
                runtime_id="runtime-current",
            ),
            owns_runtime=True,
        )

        with pytest.raises(AdmissionError) as stale:
            await session.act(
                "read",
                {"value": 1},
                request_id="request-stale",
                expected_runtime_id="runtime-old",
            )
        assert stale.value.reason_code == "STALE_RUNTIME"
        await session.close()

    _run(scenario)
