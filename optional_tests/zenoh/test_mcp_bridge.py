"""Native loopback Zenoh → HTTP MCP → Entryplug operation and evidence."""

import asyncio
import json
import socket

import pytest
import zenoh
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from entryplug_app.mcp_port import mcp_port
from entryplug_app.service import ApplicationService
from entryplug_app.workspace import Workspace
from entryplug_bridges.zenoh import TOOL, Observations, ZenohReader, create_app
from entryplug_mcp.loopback import start_loopback_server


@pytest.mark.asyncio
async def test_native_publication_imports_as_read_only_and_stale_read_fails(tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        endpoint = f"tcp/127.0.0.1:{listener.getsockname()[1]}"
    config = zenoh.Config.from_json5(
        json.dumps(
            {
                "mode": "peer",
                "listen": {"endpoints": [endpoint]},
                "scouting": {"multicast": {"enabled": False}},
            }
        )
    )
    cache = Observations(["demo/temperature"], 5.0)
    service = ApplicationService(Workspace.resolve(str(tmp_path)))
    await service.open()
    try:
        with zenoh.open(config) as publisher:
            reader = ZenohReader(endpoint, cache)
            server = start_loopback_server(create_app(cache))
            try:
                url = f"http://127.0.0.1:{server.port}/mcp"
                async with Client(streamable_http_client(url)) as client:
                    catalog = (await client.list_tools()).tools
                    assert [tool.name for tool in catalog] == [TOOL]
                    assert catalog[0].annotations.read_only_hint is True
                    assert catalog[0].input_schema["properties"]["key"]["enum"] == [
                        "demo/temperature"
                    ]
                    assert (await client.call_tool(TOOL, {"key": "demo/temperature"})).is_error
                    async with asyncio.timeout(5):
                        while cache.read("demo/temperature")["status"] != "ok":
                            publisher.put(
                                "demo/temperature", '{"simulated":true,"temperature_c":22}'
                            )
                            await asyncio.sleep(0.02)
                    reply = await client.call_tool(TOOL, {"key": "demo/temperature"})
                    assert not reply.is_error
                    assert reply.structured_content["value"]["temperature_c"] == 22
                    assert reply.structured_content["source_age_s"] is None
                    assert (await client.call_tool(TOOL, {"key": "private/secret"})).is_error
                    assert (
                        await client.call_tool("zenoh_publish", {"key": "demo/temperature"})
                    ).is_error
                port = await mcp_port(
                    {"id": "bridge", "name": "Native bridge fixture", "url": url, "tools": [TOOL]},
                    None,
                )
                service.ports[port.body_id] = port
                grant = await service.command(
                    "attachment.create",
                    {"name": "Read bridge", "body_id": port.body_id, "allow": ["mcp." + TOOL]},
                    "grant",
                )

                async def read(request_id):
                    op = await service.command(
                        "operation.create",
                        {
                            "attachment_id": grant["id"],
                            "capability": "mcp." + TOOL,
                            "arguments": {"key": "demo/temperature"},
                        },
                        request_id,
                    )
                    async with asyncio.timeout(5):
                        while True:
                            current = await service.store.get("operations", op["id"])
                            if current["lifecycle"] in {"succeeded", "failed"}:
                                return current
                            await asyncio.sleep(0.01)

                publisher.put("demo/temperature", '{"simulated":true,"temperature_c":22}')
                await asyncio.sleep(0.05)
                operation = await read("fresh")
                assert operation["lifecycle"] == "succeeded" and operation["evidence_ids"]
                data = operation["result"]["remote_result"]["structuredContent"]
                assert data["provenance"] == "zenoh_publication" and data["value"]["simulated"]
                await asyncio.sleep(5.1)
                stale = await read("stale")
                assert stale["lifecycle"] == "failed"
                assert stale["result"]["remote_result"]["structuredContent"]["status"] == "stale"
                await port.close()
                service.ports.pop(port.body_id)
            finally:
                reader.close()
                server.close()
    finally:
        await service.close()
