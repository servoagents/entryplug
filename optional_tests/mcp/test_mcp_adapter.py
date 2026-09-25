from __future__ import annotations

import asyncio
import importlib.metadata
import socket
import threading
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager

import uvicorn
from mcp import Client
from mcp.server.lowlevel import Server

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
from entryplug_mcp import CANCEL_TOOL, CATALOG_TOOL, INSPECT_TOOL, create_mcp_server


def _arguments(value: Mapping[str, JsonValue]) -> Mapping[str, object]:
    if set(value) != {"value"} or not isinstance(value["value"], int):
        raise ValueError("value must be one integer")
    return {"value": value["value"]}


def _fixture() -> tuple[Server[Session], OperationHost, list[int]]:
    calls: list[int] = []

    async def record(_: OperationContext, arguments: Mapping[str, JsonValue]) -> OperationResult:
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
                "Record one integer",
                False,
                1.0,
                0.1,
                _arguments,
                record,
                {
                    "type": "object",
                    "properties": {"value": {"type": "integer"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {"recorded": {"type": "integer"}},
                    "required": ["recorded"],
                    "additionalProperties": False,
                },
            ),
        ),
        runtime_id="mcp-runtime-1",
    )

    @asynccontextmanager
    async def lifespan(_: Server[Session]) -> AsyncIterator[Session]:
        session = Session(host, owns_runtime=False)
        try:
            yield session
        finally:
            await session.close()

    return create_mcp_server(lifespan), host, calls


async def _completed_result(client: Client, runtime_id: str, operation_id: str):
    for _ in range(20):
        inspected = await client.call_tool(
            INSPECT_TOOL,
            {
                "runtime_id": runtime_id,
                "operation_id": operation_id,
                "detail": "result",
            },
        )
        payload = inspected.structured_content
        if payload["lifecycle"] in {"succeeded", "failed", "canceled", "indeterminate"}:
            return inspected
        await asyncio.sleep(0.01)
    raise AssertionError("operation did not complete")


def test_official_client_uses_generated_tools_and_one_operation_ledger() -> None:
    assert importlib.metadata.version("mcp") == "2.2.0"
    server, host, calls = _fixture()

    async def scenario() -> None:
        async with Client(server) as client:
            listed = await client.list_tools()
            assert [tool.name for tool in listed.tools] == sorted(
                [CANCEL_TOOL, CATALOG_TOOL, INSPECT_TOOL, "record"]
            )
            record_tool = next(tool for tool in listed.tools if tool.name == "record")
            assert record_tool.input_schema == {
                "type": "object",
                "properties": {
                    "value": {"type": "integer"},
                    "request_id": {"type": "string", "minLength": 1},
                    "runtime_id": {"type": "string", "minLength": 1},
                },
                "required": ["request_id", "runtime_id", "value"],
                "additionalProperties": False,
            }

            catalog = await client.call_tool(CATALOG_TOOL, {})
            assert not catalog.is_error
            assert catalog.structured_content["runtime_id"] == "mcp-runtime-1"

            arguments = {
                "runtime_id": "mcp-runtime-1",
                "request_id": "stable-request",
                "value": 42,
            }
            accepted = await client.call_tool("record", arguments)
            repeated = await client.call_tool("record", arguments)
            assert not accepted.is_error
            assert (
                repeated.structured_content["operation_id"]
                == accepted.structured_content["operation_id"]
            )

            completed = await _completed_result(
                client,
                "mcp-runtime-1",
                accepted.structured_content["operation_id"],
            )
            assert completed.structured_content["result"] == {"recorded": 42}

            conflict = await client.call_tool("record", {**arguments, "value": 43})
            assert conflict.is_error
            assert conflict.structured_content["error"]["reason_code"] == "REQUEST_CONFLICT"

            stale = await client.call_tool(
                "record",
                {**arguments, "request_id": "stale-request", "runtime_id": "old-runtime"},
            )
            assert stale.is_error
            assert stale.structured_content["error"]["reason_code"] == "STALE_RUNTIME"

    asyncio.run(scenario())
    assert calls == [42]
    assert len(host.observe().operations) == 1
    asyncio.run(host.close())


def _start_loopback(app) -> tuple[uvicorn.Server, threading.Thread, int]:
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = int(listener.getsockname()[1])
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="error",
            lifespan="on",
        )
    )
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [listener]},
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 5.0
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    return server, thread, port


def test_streamable_http_round_trip_uses_the_same_host() -> None:
    mcp_server, host, calls = _fixture()
    app = mcp_server.streamable_http_app(json_response=True, stateless_http=True)
    server, thread, port = _start_loopback(app)

    async def scenario() -> None:
        async with Client(f"http://127.0.0.1:{port}/mcp") as client:
            accepted = await client.call_tool(
                "record",
                {
                    "runtime_id": "mcp-runtime-1",
                    "request_id": "http-request",
                    "value": 7,
                },
            )
            assert not accepted.is_error
            completed = await _completed_result(
                client,
                "mcp-runtime-1",
                accepted.structured_content["operation_id"],
            )
            assert completed.structured_content["result"] == {"recorded": 7}

    try:
        asyncio.run(scenario())
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert calls == [7]
    assert len(host.observe().operations) == 1
    asyncio.run(host.close())


def test_cancel_tool_uses_the_host_cancellation_contract() -> None:
    async def blocking(context: OperationContext, _: Mapping[str, JsonValue]) -> OperationResult:
        context.report("waiting", MotionState.MOVING)
        await context.wait_for_cancel()
        return OperationResult(Lifecycle.CANCELED, MotionState.IDLE)

    host = OperationHost(
        (
            CapabilitySpec(
                "blocking",
                "1",
                "Wait until canceled",
                True,
                1.0,
                0.2,
                lambda arguments: arguments,
                blocking,
                {"type": "object", "properties": {}, "additionalProperties": False},
            ),
        ),
        runtime_id="mcp-cancel-runtime",
    )

    @asynccontextmanager
    async def lifespan(_: Server[Session]) -> AsyncIterator[Session]:
        yield Session(host, owns_runtime=False)

    async def scenario() -> None:
        async with Client(create_mcp_server(lifespan)) as client:
            accepted = await client.call_tool(
                "blocking",
                {
                    "runtime_id": "mcp-cancel-runtime",
                    "request_id": "cancel-request",
                },
            )
            operation_id = accepted.structured_content["operation_id"]
            canceled = await client.call_tool(
                CANCEL_TOOL,
                {
                    "runtime_id": "mcp-cancel-runtime",
                    "operation_id": operation_id,
                },
            )
            assert not canceled.is_error
            completed = await _completed_result(
                client,
                "mcp-cancel-runtime",
                operation_id,
            )
            assert completed.structured_content["lifecycle"] == "canceled"
            assert completed.structured_content["motion_state"] == "idle"

    asyncio.run(scenario())
    assert len(host.observe().operations) == 1
    asyncio.run(host.close())
