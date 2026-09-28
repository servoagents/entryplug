"""Portable contract tests for non-motion physical effects and core authority."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

import pytest

from entryplug.core.evidence import JsonValue, MemoryEvidenceCache
from entryplug.core.operation import (
    AdmissionError,
    CapabilitySpec,
    EffectState,
    Lifecycle,
    MotionState,
    OperationContext,
    OperationHost,
    OperationResult,
)
from entryplug.core.reconfiguration import EvidenceCacheSlot
from entryplug.harness.session import Session


def _arguments(value: Mapping[str, JsonValue]) -> Mapping[str, object]:
    if set(value) != {"value"} or type(value["value"]) is not int:
        raise ValueError("value must be one integer")
    return {"value": value["value"]}


def _run(scenario) -> None:
    asyncio.run(scenario())


def test_light_effect_serializes_with_motion_but_not_read_or_compute() -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        writes = 0

        async def light(
            context: OperationContext, arguments: Mapping[str, JsonValue]
        ) -> OperationResult:
            nonlocal writes
            context.report_effect(EffectState.REQUESTED)
            entered.set()
            await release.wait()
            writes += 1
            context.report_effect(EffectState.REPORTED)
            return OperationResult(
                Lifecycle.SUCCEEDED,
                MotionState.IDLE,
                {"brightness": arguments["value"]},
                effect_state=EffectState.OBSERVED,
            )

        async def pure(_: OperationContext, arguments: Mapping[str, JsonValue]) -> OperationResult:
            return OperationResult(Lifecycle.SUCCEEDED, MotionState.IDLE, dict(arguments))

        async def move(_: OperationContext, __: Mapping[str, JsonValue]) -> OperationResult:
            raise AssertionError("motion must not start while light owns physical effects")

        host = OperationHost(
            (
                CapabilitySpec(
                    "set_light",
                    "1",
                    "Set light",
                    False,
                    1.0,
                    0.1,
                    _arguments,
                    light,
                    physical_effects=True,
                ),
                CapabilitySpec("compute", "1", "Pure compute", False, 1.0, 0.1, _arguments, pure),
                CapabilitySpec("read", "1", "Read", False, 1.0, 0.1, _arguments, pure),
                CapabilitySpec("move", "1", "Move", True, 1.0, 0.1, _arguments, move),
            ),
            runtime_id="fixture-a",
        )
        session = Session(host, owns_runtime=True)
        catalog = {item["name"]: item for item in (await session.observe()).capabilities}
        assert catalog["set_light"]["physical_effects"] is True
        assert catalog["move"]["physical_effects"] is True
        assert catalog["read"]["physical_effects"] is False
        operation = await session.act("set_light", {"value": 50}, request_id="stable")
        await entered.wait()
        assert (await session.observe()).active_effect_operation == operation.operation_id
        repeated = await session.act("set_light", {"value": 50}, request_id="stable")
        assert repeated.operation_id == operation.operation_id
        for capability in ("set_light", "move"):
            with pytest.raises(AdmissionError) as busy:
                await session.act(capability, {"value": 70}, request_id=f"busy-{capability}")
            assert busy.value.reason_code == "EFFECT_BUSY"
        for capability in ("read", "compute"):
            independent = await session.act(capability, {"value": 3})
            assert (await session.wait(independent, 0.2)).lifecycle == Lifecycle.SUCCEEDED
        cache = EvidenceCacheSlot(host, MemoryEvidenceCache())
        with pytest.raises(AdmissionError) as barrier:
            cache.replace(MemoryEvidenceCache(), expected_generation=1)
        assert barrier.value.reason_code == "BUSY"
        release.set()
        done = await session.wait(operation, 0.2)
        assert done.lifecycle == Lifecycle.SUCCEEDED
        assert done.motion_state == MotionState.IDLE
        assert done.effect_state == EffectState.OBSERVED
        assert writes == 1
        assert (await session.inspect(done))["effect_state"] == "observed"
        assert (await session.observe()).active_effect_operation is None
        replacement = cache.replace(MemoryEvidenceCache(), expected_generation=1)
        assert replacement.generation == 2
        assert (
            await session.act("set_light", {"value": 50}, request_id="stable")
        ).operation_id == (operation.operation_id)
        assert writes == 1
        cache.close()
        await session.close()

    _run(scenario)


def test_unresolved_light_effect_inhibits_writes_without_inventing_arm_motion() -> None:
    async def scenario() -> None:
        entered = asyncio.Event()

        async def hung(context: OperationContext, _: Mapping[str, JsonValue]) -> OperationResult:
            context.report_effect(EffectState.REQUESTED)
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def read(_: OperationContext, arguments: Mapping[str, JsonValue]) -> OperationResult:
            return OperationResult(Lifecycle.SUCCEEDED, MotionState.IDLE, dict(arguments))

        host = OperationHost(
            (
                CapabilitySpec(
                    "set_light",
                    "1",
                    "Set light",
                    False,
                    0.02,
                    0.02,
                    _arguments,
                    hung,
                    physical_effects=True,
                ),
                CapabilitySpec("read", "1", "Read", False, 1.0, 0.1, _arguments, read),
            ),
            runtime_id="fixture-b",
        )
        first = await host.start(
            "set_light", {"value": 50}, request_id="same", expected_runtime_id="fixture-b"
        )
        await entered.wait()
        done = await host.wait(first.operation_id, 0.2)
        assert done.lifecycle == Lifecycle.INDETERMINATE
        assert done.motion_state == MotionState.IDLE
        assert done.effect_state == EffectState.UNKNOWN
        assert host.observe().motion_inhibited_reason is None
        assert host.observe().effect_inhibited_reason == "STOP_UNCONFIRMED"
        repeated = await host.start(
            "set_light", {"value": 50}, request_id="same", expected_runtime_id="fixture-b"
        )
        assert repeated.operation_id == first.operation_id
        with pytest.raises(AdmissionError) as inhibited:
            await host.start(
                "set_light", {"value": 75}, request_id="new", expected_runtime_id="fixture-b"
            )
        assert inhibited.value.reason_code == "EFFECT_INHIBITED"
        with pytest.raises(AdmissionError) as barrier:
            with host.reconfiguration("memory", expected_runtime_id="fixture-b"):
                raise AssertionError("uncertain physical effects must block replacement")
        assert barrier.value.reason_code == "PHYSICAL_STATE_UNKNOWN"
        independent = await host.start(
            "read", {"value": 1}, request_id="read", expected_runtime_id="fixture-b"
        )
        assert (await host.wait(independent.operation_id, 0.2)).lifecycle == Lifecycle.SUCCEEDED
        await host.close()

    _run(scenario)


def test_cancel_does_not_roll_back_reported_light_state() -> None:
    async def scenario() -> None:
        applied = asyncio.Event()
        light_state = {"brightness": 0}

        async def set_light(
            context: OperationContext, arguments: Mapping[str, JsonValue]
        ) -> OperationResult:
            context.report_effect(EffectState.REQUESTED)
            light_state["brightness"] = int(arguments["value"])
            context.report_effect(EffectState.REPORTED)
            applied.set()
            await context.wait_for_cancel()
            return OperationResult(
                Lifecycle.CANCELED,
                MotionState.IDLE,
                {"last_brightness": light_state["brightness"]},
                effect_state=EffectState.REPORTED,
            )

        host = OperationHost(
            (
                CapabilitySpec(
                    "set_light",
                    "1",
                    "Set light",
                    False,
                    1.0,
                    0.1,
                    _arguments,
                    set_light,
                    physical_effects=True,
                ),
            ),
            runtime_id="fixture-c",
        )
        operation = await host.start(
            "set_light", {"value": 50}, request_id="first", expected_runtime_id="fixture-c"
        )
        await applied.wait()
        await host.cancel(operation.operation_id)
        done = await host.wait(operation.operation_id, 0.2)
        assert done.lifecycle == Lifecycle.CANCELED
        assert done.motion_state == MotionState.IDLE
        assert done.effect_state == EffectState.REPORTED
        assert done.result["last_brightness"] == 50
        assert light_state["brightness"] == 50
        assert host.observe().effect_inhibited_reason is None
        await host.close()

    _run(scenario)


def test_unexpected_device_error_does_not_silently_clear_pending_effect() -> None:
    async def scenario() -> None:
        async def failed(context: OperationContext, _: Mapping[str, JsonValue]) -> OperationResult:
            context.report_effect(EffectState.REQUESTED)
            raise OSError("device reply lost")

        host = OperationHost(
            (
                CapabilitySpec(
                    "set_light",
                    "1",
                    "Set light",
                    False,
                    1.0,
                    0.1,
                    _arguments,
                    failed,
                    physical_effects=True,
                ),
            ),
            runtime_id="fixture-d",
        )
        operation = await host.start(
            "set_light", {"value": 50}, request_id="first", expected_runtime_id="fixture-d"
        )
        done = await host.wait(operation.operation_id, 0.2)
        assert done.lifecycle == Lifecycle.INDETERMINATE
        assert done.motion_state == MotionState.IDLE
        assert done.effect_state == EffectState.UNKNOWN
        assert done.result["error_type"] == "OSError"
        assert host.observe().effect_inhibited_reason == "HANDLER_FAILED"
        await host.close()

    _run(scenario)


def test_motion_reserves_effect_slot_and_read_only_cannot_claim_a_write() -> None:
    async def scenario() -> None:
        release = asyncio.Event()

        async def move(context: OperationContext, _: Mapping[str, JsonValue]) -> OperationResult:
            context.report("moving", MotionState.MOVING)
            await release.wait()
            return OperationResult(Lifecycle.SUCCEEDED, MotionState.HOLDING)

        async def light(_: OperationContext, __: Mapping[str, JsonValue]) -> OperationResult:
            raise AssertionError("light must not start while motion owns effects")

        async def misdeclared(
            context: OperationContext, _: Mapping[str, JsonValue]
        ) -> OperationResult:
            context.report_effect(EffectState.REQUESTED)
            raise AssertionError("read-only work must not report a write")

        host = OperationHost(
            (
                CapabilitySpec("move", "1", "Move", True, 1.0, 0.1, _arguments, move),
                CapabilitySpec(
                    "set_light",
                    "1",
                    "Set light",
                    False,
                    1.0,
                    0.1,
                    _arguments,
                    light,
                    physical_effects=True,
                ),
                CapabilitySpec(
                    "read",
                    "1",
                    "Read",
                    False,
                    1.0,
                    0.1,
                    _arguments,
                    misdeclared,
                ),
            ),
            runtime_id="fixture-e",
        )
        moving = await host.start(
            "move", {"value": 1}, request_id="moving", expected_runtime_id="fixture-e"
        )
        assert host.observe().active_effect_operation == moving.operation_id
        with pytest.raises(AdmissionError) as blocked:
            await host.start(
                "set_light", {"value": 50}, request_id="light", expected_runtime_id="fixture-e"
            )
        assert blocked.value.reason_code == "EFFECT_BUSY"
        with pytest.raises(AdmissionError) as busy:
            await host.start(
                "move", {"value": 2}, request_id="move-again", expected_runtime_id="fixture-e"
            )
        assert busy.value.reason_code == "BUSY"
        read = await host.start(
            "read", {"value": 1}, request_id="read", expected_runtime_id="fixture-e"
        )
        failed = await host.wait(read.operation_id, 0.2)
        assert failed.lifecycle == Lifecycle.FAILED
        assert failed.result["error_type"] == "ValueError"
        release.set()
        assert (await host.wait(moving.operation_id, 0.2)).lifecycle == Lifecycle.SUCCEEDED
        await host.close()

    _run(scenario)


def test_unknown_arm_result_cannot_claim_success_or_release_effect_authority() -> None:
    async def scenario() -> None:
        async def bad_reply(_: OperationContext, __: Mapping[str, JsonValue]) -> OperationResult:
            return OperationResult(Lifecycle.SUCCEEDED, MotionState.UNKNOWN)

        host = OperationHost(
            (CapabilitySpec("move", "1", "Move", True, 1.0, 0.1, _arguments, bad_reply),),
            runtime_id="fixture-f",
        )
        operation = await host.start(
            "move", {"value": 1}, request_id="first", expected_runtime_id="fixture-f"
        )
        done = await host.wait(operation.operation_id, 0.2)
        assert done.lifecycle == Lifecycle.INDETERMINATE
        assert done.motion_state == MotionState.UNKNOWN
        assert done.effect_state == EffectState.UNKNOWN
        assert done.reason_code == "STOP_UNCONFIRMED"
        assert host.observe().motion_inhibited_reason == "STOP_UNCONFIRMED"
        assert host.observe().effect_inhibited_reason == "STOP_UNCONFIRMED"
        with pytest.raises(AdmissionError) as blocked:
            await host.start(
                "move", {"value": 2}, request_id="second", expected_runtime_id="fixture-f"
            )
        assert blocked.value.reason_code == "MOTION_INHIBITED"
        await host.close()

    _run(scenario)
