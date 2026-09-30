"""Official clients use resident application operations across transport lifetimes."""

import asyncio
import socket
from contextlib import asynccontextmanager

import httpx
import httpx2
import pytest
import uvicorn
from a2a.client import ClientConfig, ClientFactory
from a2a.helpers import new_data_part
from a2a.types import AgentCard, Message, Role, SendMessageConfiguration, SendMessageRequest
from a2a.utils import TransportProtocol
from google.protobuf.json_format import MessageToDict, ParseDict
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from entryplug_app.service import ApplicationService
from entryplug_app.workspace import Workspace
from entryplug_mcp import CATALOG_TOOL, INSPECT_TOOL
from entryplug_server.app import create_app


@asynccontextmanager
async def resident(tmp_path):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    service = ApplicationService(Workspace.resolve(str(tmp_path)))
    app = create_app(service, base_url=url)
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    await task
                await asyncio.sleep(0.01)
        attachment = await service.command(
            "attachment.create",
            {
                "name": "Protocol fixture",
                "body_id": "demo.access-camera",
                "allow": ["camera.snapshot"],
            },
            "attachment",
        )
        yield service, url, attachment
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        listener.close()


@pytest.mark.asyncio
async def test_mcp_reconnect_keeps_owner_and_same_application_request(tmp_path):
    async with resident(tmp_path) as (service, url, attachment):
        async with httpx.AsyncClient(
            headers={"Authorization": "Bearer " + attachment["token"]}
        ) as api:
            response = await api.post(
                f"{url}/v1/operations",
                headers={"Idempotency-Key": "same-app-request"},
                json={"attachment_id": attachment["id"], "capability": "camera.snapshot"},
            )
            assert response.status_code == 202
            initial = response.json()["id"]
        ids = []
        async with httpx2.AsyncClient(
            headers={"Authorization": "Bearer " + attachment["token"]}
        ) as http:
            for _ in range(2):
                async with Client(
                    streamable_http_client(f"{url}/mcp/{attachment['id']}/", http_client=http)
                ) as client:
                    catalog = (await client.call_tool(CATALOG_TOOL, {})).structured_content
                    accepted = await client.call_tool(
                        "camera.snapshot",
                        {"runtime_id": catalog["runtime_id"], "request_id": "same-app-request"},
                    )
                    assert not accepted.is_error, accepted
                    identifier = accepted.structured_content["operation_id"]
                    ids.append(identifier)
                    for _ in range(100):
                        result = await client.call_tool(
                            INSPECT_TOOL,
                            {
                                "runtime_id": catalog["runtime_id"],
                                "operation_id": identifier,
                                "detail": "result",
                            },
                        )
                        if result.structured_content["lifecycle"] == "succeeded":
                            break
                        await asyncio.sleep(0.01)
                    assert result.structured_content["evidence_ids"]
        assert initial == ids[0] == ids[1]
        assert not service.closed
        assert len(await service.store.list("operations")) == 1
        async with httpx.AsyncClient(
            headers={"Authorization": "Bearer " + attachment["token"]}
        ) as api:
            op = (await api.get(f"{url}/v1/operations/{ids[0]}")).json()
            assert op["lifecycle"] == "succeeded"
            await service.command("attachment.revoke", {"attachment_id": attachment["id"]}, "bye")
            assert (await api.get(f"{url}/mcp/{attachment['id']}/")).status_code == 401


@pytest.mark.asyncio
async def test_a2a_task_links_to_resident_operation_and_evidence(tmp_path):
    async with resident(tmp_path) as (service, url, attachment):
        async with httpx.AsyncClient(
            headers={"Authorization": "Bearer " + attachment["token"]}
        ) as http:
            response = await http.get(f"{url}/a2a/{attachment['id']}/.well-known/agent-card.json")
            assert response.status_code == 200, response.text
            card = ParseDict(response.json(), AgentCard())
            client = ClientFactory(
                ClientConfig(
                    streaming=False,
                    polling=False,
                    httpx_client=http,
                    supported_protocol_bindings=[TransportProtocol.HTTP_JSON],
                )
            ).create(card)
            runtime = (await service.ports["demo.access-camera"].session.observe()).runtime_id
            events = [
                event
                async for event in client.send_message(
                    SendMessageRequest(
                        message=Message(
                            role=Role.ROLE_USER,
                            message_id="message-1",
                            parts=[
                                new_data_part(
                                    {
                                        "capability": "camera.snapshot",
                                        "runtime_id": runtime,
                                        "request_id": "a2a-request",
                                        "arguments": {},
                                    },
                                    media_type="application/json",
                                )
                            ],
                        ),
                        configuration=SendMessageConfiguration(return_immediately=False),
                    )
                )
            ]
            task = events[0].task
            artifact = MessageToDict(task.artifacts[0].parts[0].data)
            op = await service.store.get("operations", artifact["operation_id"])
            assert artifact["lifecycle"] == op["lifecycle"] == "succeeded"
            assert artifact["evidence_ids"] == op["evidence_ids"]
            assert artifact["native_operation_id"] == op["native_operation_id"]
            await client.close()
        assert not service.closed


@pytest.mark.asyncio
async def test_a2a_cancel_preserves_unconfirmed_physical_result(tmp_path):
    from a2a.types import CancelTaskRequest, TaskState

    from entryplug.core.operation import (
        CapabilitySpec,
        EffectState,
        Lifecycle,
        MotionState,
        OperationHost,
        OperationResult,
    )
    from entryplug.harness.session import Session
    from entryplug_app.ports import EmbodimentPort

    async with resident(tmp_path) as (service, url, _):

        async def effect(context, arguments):
            context.report_effect(EffectState.REQUESTED)
            await context.wait_for_cancel()
            return OperationResult(
                Lifecycle.INDETERMINATE,
                MotionState.IDLE,
                reason_code="CANCEL_UNCONFIRMED",
                effect_state=EffectState.UNKNOWN,
            )

        spec = CapabilitySpec(
            "fixture.effect",
            "1",
            "Uncertain-effect fixture",
            False,
            5,
            0.2,
            lambda a: a,
            effect,
            {"type": "object", "properties": {}},
            physical_effects=True,
        )
        port = EmbodimentPort(
            "effect-fixture",
            "Effect fixture",
            Session(OperationHost([spec]), owns_runtime=True),
            simulated=True,
        )
        service.ports[port.body_id] = port
        attachment = await service.command(
            "attachment.create",
            {"name": "Cancel fixture", "body_id": port.body_id, "allow": ["fixture.effect"]},
            "effect-grant",
        )
        async with httpx.AsyncClient(
            headers={"Authorization": "Bearer " + attachment["token"]}
        ) as http:
            card = ParseDict(
                (
                    await http.get(f"{url}/a2a/{attachment['id']}/.well-known/agent-card.json")
                ).json(),
                AgentCard(),
            )
            client = ClientFactory(
                ClientConfig(
                    streaming=False,
                    polling=False,
                    httpx_client=http,
                    supported_protocol_bindings=[TransportProtocol.HTTP_JSON],
                )
            ).create(card)
            events = [
                event
                async for event in client.send_message(
                    SendMessageRequest(
                        message=Message(
                            role=Role.ROLE_USER,
                            message_id="uncertain-cancel",
                            parts=[
                                new_data_part(
                                    {
                                        "capability": "fixture.effect",
                                        "runtime_id": (await port.session.observe()).runtime_id,
                                        "request_id": "uncertain-cancel",
                                        "arguments": {},
                                    },
                                    media_type="application/json",
                                )
                            ],
                        ),
                        configuration=SendMessageConfiguration(return_immediately=True),
                    )
                )
            ]
            async with asyncio.timeout(2):
                while not (await port.session.observe()).operations:
                    await asyncio.sleep(0.01)
            canceled = await client.cancel_task(CancelTaskRequest(id=events[0].task.id))
            assert canceled.status.state == TaskState.TASK_STATE_FAILED
            artifact = MessageToDict(canceled.artifacts[0].parts[0].data)
            assert artifact["lifecycle"] == "indeterminate"
            assert artifact["effect_state"] == "unknown"
            assert artifact["evidence_ids"]
            await client.close()
