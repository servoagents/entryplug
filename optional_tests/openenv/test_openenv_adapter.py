from __future__ import annotations

import asyncio
import importlib.metadata
import socket
import threading
import time
from collections.abc import Mapping

import uvicorn

from entryplug.agent import Act, Inspect, Wait
from entryplug.episode import EpisodeController, EpisodeStart
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
from entryplug_openenv import (
    ActDecision,
    EntryplugAction,
    EntryplugEnvClient,
    EntryplugEnvironment,
    InspectDecision,
    StopDecision,
    WaitDecision,
    create_entryplug_app,
)


def _arguments(value: Mapping[str, JsonValue]) -> Mapping[str, object]:
    if set(value) != {"value"} or not isinstance(value["value"], int):
        raise ValueError("value must be one integer")
    return {"value": value["value"]}


def _fixture(
    hosts: list[OperationHost],
    calls: list[int],
):
    async def factory(seed: int) -> EpisodeStart:
        async def record(
            _: OperationContext, arguments: Mapping[str, JsonValue]
        ) -> OperationResult:
            calls.append(int(arguments["value"]))
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
            runtime_id=f"openenv-runtime-{seed}-{len(hosts)}",
        )
        hosts.append(host)
        return EpisodeStart(
            Session(host, owns_runtime=True),
            {"fixture": "openenv-contract", "seed": seed},
        )

    return factory


def _port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def test_sdk_client_and_direct_core_produce_equivalent_public_results() -> None:
    assert importlib.metadata.version("openenv") == "0.5.0"
    hosts: list[OperationHost] = []
    calls: list[int] = []
    fixture = _fixture(hosts, calls)
    app = create_entryplug_app(
        lambda: EntryplugEnvironment(
            fixture,
            episode_timeout_seconds=5.0,
            observation_window_seconds=1.0,
        )
    )
    port = _port()
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="error",
            lifespan="on",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5.0
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started

    async def through_openenv() -> tuple[dict[str, object], str]:
        async with EntryplugEnvClient(base_url=f"http://127.0.0.1:{port}") as client:
            initial = await client.reset(seed=17)
            assert initial.observation.state.seed == 17
            assert initial.observation.reward is None

            action = EntryplugAction(
                decision=ActDecision(
                    capability="record",
                    arguments={"value": 42},
                    request_id="stable-openenv-request",
                )
            )
            accepted = await client.step(action)
            repeated = await client.step(action)
            operation_id = str(accepted.observation.result["operation_id"])
            assert repeated.observation.result["operation_id"] == operation_id

            completed = await client.step(
                EntryplugAction(
                    decision=WaitDecision(
                        operation_id=operation_id,
                        timeout_seconds=0.5,
                    )
                )
            )
            assert completed.observation.result["lifecycle"] == "succeeded"
            inspected = await client.step(
                EntryplugAction(decision=InspectDecision(reference=operation_id, detail="result"))
            )
            stopped = await client.step(
                EntryplugAction(decision=StopDecision(reason="client complete"))
            )
            state = await client.state()

            assert stopped.done
            assert stopped.reward is None
            assert state.step_count == 5
            assert state.decision_count == 5
            assert state.wait_count == 1
            assert state.elapsed_sim_time_seconds is None
            assert state.public_metadata == {
                "fixture": "openenv-contract",
                "seed": 17,
            }
            return inspected.observation.result, state.runtime_id

    try:
        openenv_result, openenv_runtime = asyncio.run(through_openenv())
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)
    assert not thread.is_alive()

    async def through_core() -> tuple[dict[str, JsonValue], str]:
        controller = EpisodeController(fixture, timeout_seconds=5.0)
        state = await controller.reset(23)
        accepted = await controller.apply(
            Act("record", {"value": 42}),
            request_id="stable-core-request",
        )
        assert accepted is not None and not isinstance(accepted, Mapping)
        await controller.apply(Wait(accepted.operation_id, 0.5))
        inspected = await controller.apply(Inspect(accepted.operation_id, "result"))
        assert isinstance(inspected, Mapping)
        await controller.close()
        return dict(inspected), state.runtime_id

    core_result, core_runtime = asyncio.run(through_core())

    assert openenv_result["result"] == core_result["result"] == {"recorded": 42}
    assert openenv_runtime.startswith("openenv-runtime-17")
    assert core_runtime.startswith("openenv-runtime-23")
    assert calls == [42, 42]
    assert len(hosts[0].observe().operations) == 1
    assert not hosts[0].observe().admission_open
