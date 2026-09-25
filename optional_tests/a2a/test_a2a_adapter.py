from __future__ import annotations

import asyncio
import importlib.metadata
from collections.abc import Mapping

import httpx
from a2a.client import ClientConfig, ClientFactory
from a2a.helpers import new_data_part
from a2a.types import (
    CancelTaskRequest,
    Message,
    Part,
    Role,
    SendMessageConfiguration,
    SendMessageRequest,
    Task,
    TaskState,
)
from a2a.utils import TransportProtocol
from google.protobuf.json_format import MessageToDict

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
from entryplug_a2a import create_a2a_server


def _arguments(value: Mapping[str, JsonValue]) -> Mapping[str, object]:
    if set(value) != {"value"} or not isinstance(value["value"], int):
        raise ValueError("value must be one integer")
    return {"value": value["value"]}


def _record_fixture() -> tuple[OperationHost, list[int]]:
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
        runtime_id="a2a-runtime-1",
    )
    return host, calls


def _message(message_id: str, **overrides: object) -> Message:
    payload: dict[str, object] = {
        "capability": "record",
        "runtime_id": "a2a-runtime-1",
        "request_id": "stable-request",
        "arguments": {"value": 42},
    }
    payload.update(overrides)
    return Message(
        role=Role.ROLE_USER,
        message_id=message_id,
        parts=[new_data_part(payload, media_type="application/json")],
    )


def _artifact(task: Task) -> dict[str, object]:
    assert len(task.artifacts) == 1
    assert task.artifacts[0].name == "entryplug-operation"
    assert len(task.artifacts[0].parts) == 1
    decoded = MessageToDict(task.artifacts[0].parts[0].data)
    assert isinstance(decoded, dict)
    return decoded


async def _send(client, message: Message, *, return_immediately: bool = False) -> Task:
    events = [
        event
        async for event in client.send_message(
            SendMessageRequest(
                message=message,
                configuration=SendMessageConfiguration(return_immediately=return_immediately),
            )
        )
    ]
    assert len(events) == 1
    assert events[0].HasField("task")
    return events[0].task


def test_official_client_uses_generated_skills_and_shared_operation_ledger() -> None:
    assert importlib.metadata.version("a2a-sdk") == "1.1.5"
    host, calls = _record_fixture()

    async def scenario() -> None:
        server = await create_a2a_server(
            Session(host, owns_runtime=False),
            base_url="http://testserver",
        )
        assert server.card.supported_interfaces[0].protocol_binding == TransportProtocol.HTTP_JSON
        assert server.card.supported_interfaces[0].protocol_version == "1.0"
        assert [skill.name for skill in server.card.skills] == ["record"]
        assert '"required":["value"]' in server.card.skills[0].description
        assert "a2a-runtime-1" in server.card.description

        http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server.app),
            base_url="http://testserver",
        )
        client = ClientFactory(
            ClientConfig(
                streaming=False,
                polling=False,
                httpx_client=http,
                supported_protocol_bindings=[TransportProtocol.HTTP_JSON],
            )
        ).create(server.card)
        try:
            card_response = await http.get("/.well-known/agent-card.json")
            assert card_response.status_code == 200
            assert card_response.json()["skills"][0]["name"] == "record"

            completed = await _send(client, _message("message-1"))
            assert completed.status.state == TaskState.TASK_STATE_COMPLETED
            first_payload = _artifact(completed)
            assert first_payload["lifecycle"] == "succeeded"
            assert first_payload["result"] == {"recorded": 42.0}

            repeated = await _send(client, _message("message-2"))
            assert repeated.status.state == TaskState.TASK_STATE_COMPLETED
            assert _artifact(repeated)["operation_id"] == first_payload["operation_id"]

            conflict = await _send(
                client,
                _message("message-3", arguments={"value": 43}),
            )
            assert conflict.status.state == TaskState.TASK_STATE_REJECTED
            assert _artifact(conflict)["error"]["reason_code"] == "REQUEST_CONFLICT"

            stale = await _send(
                client,
                _message(
                    "message-4",
                    runtime_id="retired-runtime",
                    request_id="stale-request",
                ),
            )
            assert stale.status.state == TaskState.TASK_STATE_REJECTED
            assert _artifact(stale)["error"]["reason_code"] == "STALE_RUNTIME"

            malformed = await _send(
                client,
                Message(
                    role=Role.ROLE_USER,
                    message_id="message-5",
                    parts=[Part(text="record 42")],
                ),
            )
            assert malformed.status.state == TaskState.TASK_STATE_REJECTED
            assert _artifact(malformed)["error"]["reason_code"] == "INVALID_ARGUMENT"
        finally:
            await client.close()
            await server.close()

    asyncio.run(scenario())
    assert calls == [42]
    assert len(host.observe().operations) == 1
    asyncio.run(host.close())


def test_cancel_task_waits_for_confirmed_operation_quiescence() -> None:
    async def blocking(context: OperationContext, _: Mapping[str, JsonValue]) -> OperationResult:
        context.report("moving", MotionState.MOVING)
        await context.wait_for_cancel()
        context.report("stopped", MotionState.IDLE)
        return OperationResult(Lifecycle.CANCELED, MotionState.IDLE)

    host = OperationHost(
        (
            CapabilitySpec(
                "blocking",
                "1",
                "Move until canceled",
                True,
                2.0,
                0.2,
                lambda arguments: arguments,
                blocking,
                {"type": "object", "properties": {}, "additionalProperties": False},
            ),
        ),
        runtime_id="a2a-cancel-runtime",
    )

    async def scenario() -> None:
        server = await create_a2a_server(
            Session(host, owns_runtime=False),
            base_url="http://testserver",
        )
        http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server.app),
            base_url="http://testserver",
        )
        client = ClientFactory(
            ClientConfig(
                streaming=False,
                polling=False,
                httpx_client=http,
                supported_protocol_bindings=[TransportProtocol.HTTP_JSON],
            )
        ).create(server.card)
        try:
            submitted = await _send(
                client,
                Message(
                    role=Role.ROLE_USER,
                    message_id="cancel-message",
                    parts=[
                        new_data_part(
                            {
                                "capability": "blocking",
                                "runtime_id": "a2a-cancel-runtime",
                                "request_id": "cancel-request",
                                "arguments": {},
                            },
                            media_type="application/json",
                        )
                    ],
                ),
                return_immediately=True,
            )
            assert submitted.status.state in {
                TaskState.TASK_STATE_SUBMITTED,
                TaskState.TASK_STATE_WORKING,
            }
            canceled = await client.cancel_task(CancelTaskRequest(id=submitted.id))
            assert canceled.status.state == TaskState.TASK_STATE_CANCELED
            payload = _artifact(canceled)
            assert payload["lifecycle"] == "canceled"
            assert payload["motion_state"] == "idle"
            assert payload["cancel_requested"] is True
        finally:
            await client.close()
            await server.close()

    asyncio.run(scenario())
    operation = host.observe().operations[0]
    assert operation.lifecycle == Lifecycle.CANCELED
    assert operation.motion_state == MotionState.IDLE
    asyncio.run(host.close())
