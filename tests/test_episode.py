from __future__ import annotations

import asyncio
from collections.abc import Mapping

import pytest

from entryplug.agent import (
    Act,
    AgentRunner,
    CatalogInspectingExplorer,
    Inspect,
    ScriptedExplorer,
    Wait,
)
from entryplug.episode import (
    EpisodeController,
    EpisodeStart,
    EpisodeStatus,
    EpisodeTimeoutError,
)
from entryplug.evidence import JsonValue
from entryplug.operation import (
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


def test_reset_closes_active_work_and_requires_a_new_runtime() -> None:
    async def scenario() -> None:
        hosts: list[OperationHost] = []

        async def factory(seed: int) -> EpisodeStart:
            async def move(
                context: OperationContext, _: Mapping[str, JsonValue]
            ) -> OperationResult:
                context.report("move", MotionState.MOVING)
                await context.wait_for_cancel()
                return OperationResult(Lifecycle.CANCELED, MotionState.HOLDING)

            host = OperationHost(
                (CapabilitySpec("move", "1", "Move", True, 2.0, 0.1, _arguments, move),),
                runtime_id=f"runtime-{seed}",
            )
            hosts.append(host)
            return EpisodeStart(
                Session(host, owns_runtime=True),
                {"fixture": "unit", "seed": seed},
            )

        episode = EpisodeController(factory, timeout_seconds=2.0)
        first = await episode.reset(1)
        runner = AgentRunner(ScriptedExplorer("move", {"value": 1}))
        await episode.step(runner)
        second = await episode.reset(2)

        old_operation = hosts[0].observe().operations[0]
        assert old_operation.lifecycle == Lifecycle.CANCELED
        assert not hosts[0].observe().admission_open
        assert first.runtime_id == "runtime-1"
        assert second.runtime_id == "runtime-2"
        assert second.generation == first.generation + 1
        assert second.step_count == 0
        assert second.public_metadata == {"fixture": "unit", "seed": 2}
        await runner.close()
        await episode.close()

    _run(scenario)


def test_episode_timeout_closes_the_owned_runtime_and_can_be_reset() -> None:
    async def scenario() -> None:
        hosts: list[OperationHost] = []

        async def factory(seed: int) -> EpisodeStart:
            async def wait_for_stop(
                context: OperationContext, _: Mapping[str, JsonValue]
            ) -> OperationResult:
                await context.wait_for_cancel()
                return OperationResult(Lifecycle.CANCELED, MotionState.HOLDING)

            host = OperationHost(
                (
                    CapabilitySpec(
                        "wait",
                        "1",
                        "Wait for cancellation",
                        True,
                        4.0,
                        0.1,
                        _arguments,
                        wait_for_stop,
                    ),
                ),
                runtime_id=f"runtime-{seed}",
            )
            hosts.append(host)
            return EpisodeStart(Session(host, owns_runtime=True), {"fixture": "timeout"})

        episode = EpisodeController(factory, timeout_seconds=1.5)
        await episode.reset(1)
        runner = AgentRunner(ScriptedExplorer("wait", {"value": 1}, wait_seconds=1.0))

        with pytest.raises(EpisodeTimeoutError, match="deadline"):
            await episode.run_until_stop(runner)

        assert episode.observe().status == EpisodeStatus.TIMED_OUT
        assert hosts[0].observe().operations[0].lifecycle == Lifecycle.CANCELED
        recovered = await episode.reset(2)
        assert recovered.status == EpisodeStatus.READY
        await runner.close()
        await episode.close()

    _run(scenario)


def test_two_isolated_policies_share_one_episode_and_operation_host() -> None:
    async def scenario() -> None:
        host: OperationHost | None = None

        async def factory(seed: int) -> EpisodeStart:
            nonlocal host

            async def record(
                _: OperationContext, arguments: Mapping[str, JsonValue]
            ) -> OperationResult:
                return OperationResult(
                    Lifecycle.SUCCEEDED,
                    MotionState.IDLE,
                    {"recorded": arguments["value"]},
                )

            host = OperationHost(
                (
                    CapabilitySpec(
                        "record",
                        "1",
                        "Record one value",
                        False,
                        1.0,
                        0.1,
                        _arguments,
                        record,
                    ),
                ),
                runtime_id=f"runtime-{seed}",
            )
            return EpisodeStart(Session(host, owns_runtime=True), {"fixture": "record"})

        episode = EpisodeController(factory, timeout_seconds=3.0)
        initial = await episode.reset(7)
        runner = AgentRunner(ScriptedExplorer("record", {"value": 11}))
        first_steps = await episode.run_until_stop(runner)
        first_process = first_steps[0].reply.agent_process_id

        await runner.replace(CatalogInspectingExplorer({"record": {"value": 22}}))
        second_steps = await episode.run_until_stop(runner)

        assert host is not None
        operations = host.observe().operations
        assert initial.runtime_id == host.runtime_id
        assert [operation.result["recorded"] for operation in operations] == [11, 22]
        assert any(isinstance(step.reply.decision, Inspect) for step in second_steps)
        assert second_steps[0].reply.agent_process_id != first_process
        assert second_steps[0].reply.agent_generation == first_steps[0].reply.agent_generation + 1
        assert episode.observe().step_count == len(first_steps) + len(second_steps)
        assert episode.observe().status == EpisodeStatus.STOPPED
        await runner.close()
        await episode.close()

    _run(scenario)


def test_external_decisions_use_the_same_episode_and_request_deduplication() -> None:
    async def scenario() -> None:
        host: OperationHost | None = None
        calls = 0

        async def factory(seed: int) -> EpisodeStart:
            nonlocal host

            async def record(
                _: OperationContext, arguments: Mapping[str, JsonValue]
            ) -> OperationResult:
                nonlocal calls
                calls += 1
                return OperationResult(
                    Lifecycle.SUCCEEDED,
                    MotionState.IDLE,
                    {"recorded": arguments["value"]},
                )

            host = OperationHost(
                (
                    CapabilitySpec(
                        "record",
                        "1",
                        "Record one value",
                        False,
                        1.0,
                        0.1,
                        _arguments,
                        record,
                    ),
                ),
                runtime_id=f"runtime-{seed}",
            )
            return EpisodeStart(Session(host, owns_runtime=True), {"fixture": "record"})

        episode = EpisodeController(factory, timeout_seconds=2.0)
        await episode.reset(9)
        decision = Act("record", {"value": 31})
        first = await episode.apply(decision, request_id="stable-request")
        repeated = await episode.apply(decision, request_id="stable-request")
        assert first is not None and not isinstance(first, Mapping)
        assert repeated is not None and not isinstance(repeated, Mapping)
        assert first.operation_id == repeated.operation_id

        completed = await episode.apply(Wait(first.operation_id, 0.5))
        assert completed is not None and not isinstance(completed, Mapping)
        assert completed.result == {"recorded": 31}
        assert calls == 1
        assert episode.observe().step_count == 3
        await episode.close()

    _run(scenario)
