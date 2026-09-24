from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Mapping

import pytest

from entryplug.agent import (
    Act,
    AgentBusyError,
    AgentDecisionTimeout,
    AgentProcessError,
    AgentRunner,
    AgentStartupTimeout,
    ScriptedExplorer,
    StaleAgentDecision,
    Stop,
    Wait,
    agent_execution_record,
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


class ChildPidPolicy:
    async def decide(self, _: Mapping[str, JsonValue]) -> Act:
        await asyncio.sleep(0)
        return Act("record_pid", {"pid": os.getpid()})


class HangingPolicy:
    def decide(self, _: Mapping[str, JsonValue]) -> Stop:
        time.sleep(0.5)
        return Stop("too late")


class SlowActPolicy:
    def decide(self, _: Mapping[str, JsonValue]) -> Act:
        time.sleep(0.1)
        return Act("record_pid", {"pid": os.getpid()})


class CrashingPolicy:
    def decide(self, _: Mapping[str, JsonValue]) -> Stop:
        os._exit(7)


class StopPolicy:
    def __init__(self, reason: str) -> None:
        self._reason = reason

    def decide(self, _: Mapping[str, JsonValue]) -> Stop:
        return Stop(self._reason)


class SlowStartingPolicy:
    def __getstate__(self) -> dict[str, float]:
        return {"delay": 0.1}

    def __setstate__(self, state: dict[str, float]) -> None:
        time.sleep(state["delay"])

    def decide(self, _: Mapping[str, JsonValue]) -> Stop:
        return Stop("ready")


def _pid_arguments(value: Mapping[str, JsonValue]) -> Mapping[str, object]:
    if set(value) != {"pid"} or not isinstance(value["pid"], int):
        raise ValueError("pid must be one integer")
    return {"pid": value["pid"]}


async def _record_pid(_: OperationContext, arguments: Mapping[str, JsonValue]) -> OperationResult:
    return OperationResult(
        Lifecycle.SUCCEEDED,
        MotionState.IDLE,
        {"pid": arguments["pid"]},
    )


def _pid_host() -> OperationHost:
    return OperationHost(
        (
            CapabilitySpec(
                "record_pid",
                "1",
                "Record a policy process identifier",
                False,
                1.0,
                0.1,
                _pid_arguments,
                _record_pid,
            ),
        ),
        runtime_id="runtime-a",
    )


def _run(scenario) -> None:
    asyncio.run(scenario())


def test_async_policy_runs_in_child_and_dispatches_through_session() -> None:
    async def scenario() -> None:
        host = _pid_host()
        session = Session(host, owns_runtime=True)
        async with AgentRunner(ChildPidPolicy()) as runner:
            step = await runner.step(session)
            assert isinstance(step.reply.decision, Act)
            operation = step.result
            assert operation is not None and not isinstance(operation, Mapping)
            completed = await session.wait(operation, 0.5)

            assert completed.lifecycle == Lifecycle.SUCCEEDED
            assert completed.result["pid"] != os.getpid()
            assert step.reply.agent_process_id == completed.result["pid"]
            assert step.reply.process_startup_ms is not None
            assert step.reply.decision_latency_ms >= 0
            assert completed.request_id
            assert step.reply.runtime_id == "runtime-a"
        await session.close()

    _run(scenario)


def test_scripted_explorer_acts_waits_in_code_and_stops_on_result() -> None:
    async def scenario() -> None:
        async def delayed(
            _: OperationContext, arguments: Mapping[str, JsonValue]
        ) -> OperationResult:
            await asyncio.sleep(0.05)
            return OperationResult(
                Lifecycle.SUCCEEDED,
                MotionState.IDLE,
                {"pid": arguments["pid"]},
            )

        host = OperationHost(
            (
                CapabilitySpec(
                    "record_pid",
                    "1",
                    "Record a supplied identifier",
                    False,
                    1.0,
                    0.1,
                    _pid_arguments,
                    delayed,
                ),
            ),
            runtime_id="runtime-a",
        )
        session = Session(host, owns_runtime=True)
        policy = ScriptedExplorer("record_pid", {"pid": 42}, wait_seconds=0.2)
        async with AgentRunner(policy) as runner:
            steps = await runner.run_until_stop(session)

        assert isinstance(steps[0].reply.decision, Act)
        assert any(isinstance(step.reply.decision, Wait) for step in steps)
        assert isinstance(steps[-1].reply.decision, Stop)
        assert host.observe().operations[-1].result == {"pid": 42}
        record = agent_execution_record(
            steps,
            host.observe().operations[-1],
            policy="scripted_test",
        )
        assert record["separate_policy_process"] is True
        assert record["wait_decision_count"] == 1
        await session.close()

    _run(scenario)


def test_policy_timeout_retires_child_without_canceling_accepted_work() -> None:
    async def scenario() -> None:
        async def delayed(
            _: OperationContext, arguments: Mapping[str, JsonValue]
        ) -> OperationResult:
            await asyncio.sleep(0.1)
            return OperationResult(
                Lifecycle.SUCCEEDED,
                MotionState.IDLE,
                {"pid": arguments["pid"]},
            )

        host = OperationHost(
            (
                CapabilitySpec(
                    "record_pid",
                    "1",
                    "Record a supplied identifier",
                    False,
                    1.0,
                    0.1,
                    _pid_arguments,
                    delayed,
                ),
            ),
            runtime_id="runtime-a",
        )
        session = Session(host, owns_runtime=True)
        operation = await session.act("record_pid", {"pid": 7})
        runner = AgentRunner(HangingPolicy(), decision_timeout_seconds=0.05)

        with pytest.raises(AgentDecisionTimeout):
            await runner.step(session)

        completed = await session.wait(operation, 0.5)
        assert completed.lifecycle == Lifecycle.SUCCEEDED
        assert runner.generation == 2
        await runner.close()
        await session.close()

    _run(scenario)


def test_process_startup_is_not_charged_to_the_decision_deadline() -> None:
    async def scenario() -> None:
        host = _pid_host()
        runner = AgentRunner(
            SlowStartingPolicy(),
            decision_timeout_seconds=0.02,
            startup_timeout_seconds=2.0,
        )

        reply = await runner.decide(host.observe())

        assert reply.decision == Stop("ready")
        await runner.close()
        await host.close()

    _run(scenario)


def test_process_startup_has_a_separate_finite_deadline() -> None:
    async def scenario() -> None:
        host = _pid_host()
        runner = AgentRunner(
            SlowStartingPolicy(),
            decision_timeout_seconds=1.0,
            startup_timeout_seconds=0.02,
        )

        with pytest.raises(AgentStartupTimeout):
            await runner.decide(host.observe())

        assert runner.generation == 2
        await runner.close()
        await host.close()

    _run(scenario)


def test_runner_allows_only_one_outstanding_decision() -> None:
    async def scenario() -> None:
        host = _pid_host()
        runner = AgentRunner(HangingPolicy(), decision_timeout_seconds=0.1)
        view = host.observe()
        pending = asyncio.create_task(runner.decide(view))
        await asyncio.sleep(0.02)

        with pytest.raises(AgentBusyError):
            await runner.decide(view)
        with pytest.raises(AgentDecisionTimeout):
            await pending

        await runner.close()
        await host.close()

    _run(scenario)


def test_canceling_an_outstanding_decision_retires_its_child() -> None:
    async def scenario() -> None:
        host = _pid_host()
        runner = AgentRunner(SlowActPolicy())
        pending = asyncio.create_task(runner.decide(host.observe()))
        await asyncio.sleep(0.02)

        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending

        assert runner.generation == 2
        await runner.replace(StopPolicy("replacement"))
        assert (await runner.decide(host.observe())).decision == Stop("replacement")
        await runner.close()
        await host.close()

    _run(scenario)


def test_crashed_policy_is_retired_and_cannot_reply_later() -> None:
    async def scenario() -> None:
        host = _pid_host()
        runner = AgentRunner(CrashingPolicy())

        with pytest.raises(AgentProcessError, match="exited"):
            await runner.decide(host.observe())
        assert runner.generation == 2

        await runner.replace(StopPolicy("recovered"))
        reply = await runner.decide(host.observe())
        assert reply.decision == Stop("recovered")
        await runner.close()
        await host.close()

    _run(scenario)


def test_decision_for_retired_runtime_is_discarded_before_action() -> None:
    async def scenario() -> None:
        host = _pid_host()
        session = Session(host, owns_runtime=True)
        runner = AgentRunner(SlowActPolicy())
        pending = asyncio.create_task(runner.step(session))
        await asyncio.sleep(0.03)
        host.runtime_id = "runtime-b"

        with pytest.raises(StaleAgentDecision):
            await pending
        assert host.observe().operations == ()

        await runner.close()
        await session.close()

    _run(scenario)


def test_idle_policy_replacement_starts_a_fresh_generation() -> None:
    async def scenario() -> None:
        host = _pid_host()
        session = Session(host, owns_runtime=True)
        runner = AgentRunner(StopPolicy("first"))

        first = await runner.step(session)
        await runner.replace(StopPolicy("second"))
        second = await runner.step(session)

        assert first.reply.decision == Stop("first")
        assert second.reply.decision == Stop("second")
        assert second.reply.agent_generation == first.reply.agent_generation + 1
        assert host.observe().operations == ()
        await runner.close()
        await session.close()

    _run(scenario)
