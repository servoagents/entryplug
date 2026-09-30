import asyncio

import pytest

from entryplug_app.a2a_port import a2a_port
from entryplug_app.mcp_port import mcp_port

from .test_protocols import resident


@pytest.mark.asyncio
async def test_outbound_mcp_preserves_structured_content_and_source_identity(tmp_path):
    async with resident(tmp_path) as (service, url, attachment):
        runtime = (await service.ports["demo.access-camera"].session.observe()).runtime_id
        port = await mcp_port(
            {
                "id": "remote-camera",
                "name": "Explicit MCP fixture",
                "url": f"{url}/mcp/{attachment['id']}/",
                "tools": ["camera.snapshot"],
            },
            attachment["token"],
        )
        service.ports[port.body_id] = port
        outbound = await service.command(
            "attachment.create",
            {"name": "Outbound fixture", "body_id": port.body_id, "allow": ["mcp.camera.snapshot"]},
            "outbound",
        )
        op = await service.command(
            "operation.create",
            {
                "attachment_id": outbound["id"],
                "capability": "mcp.camera.snapshot",
                "arguments": {"runtime_id": runtime, "request_id": "outbound-request"},
            },
            "call-outbound",
        )
        for _ in range(100):
            current = await service.store.get("operations", op["id"])
            if current["lifecycle"] == "succeeded":
                break
            await asyncio.sleep(0.01)
        assert current["lifecycle"] == "succeeded", current
        result = current["result"]
        assert result["provenance"] == "remote_report"
        assert result["remote_result"]["structuredContent"]["runtime_id"] == runtime
        assert result["remote_result"]["content"][0]["type"] == "text"
        assert result["catalog_hash"] and current["evidence_ids"]
        await port.close()
        service.ports.pop(port.body_id)
        assert not service.closed


@pytest.mark.asyncio
async def test_outbound_a2a_retains_task_context_operation_and_evidence(tmp_path):
    async with resident(tmp_path) as (service, url, attachment):
        port = await a2a_port(
            {
                "id": "delegated-camera",
                "name": "One-hop A2A fixture",
                "url": f"{url}/a2a/{attachment['id']}/.well-known/agent-card.json",
                "tools": ["camera.snapshot"],
            },
            attachment["token"],
        )
        service.ports[port.body_id] = port
        grant = await service.command(
            "attachment.create",
            {
                "name": "Delegation fixture",
                "body_id": port.body_id,
                "allow": ["a2a.camera.snapshot"],
            },
            "delegate-grant",
        )
        op = await service.command(
            "operation.create",
            {"attachment_id": grant["id"], "capability": "a2a.camera.snapshot"},
            "delegate",
        )
        for _ in range(200):
            current = await service.store.get("operations", op["id"])
            if current["lifecycle"] in {"succeeded", "failed", "indeterminate"}:
                break
            await asyncio.sleep(0.01)
        assert current["lifecycle"] == "succeeded", current
        reference = current["result"]["delegation"]
        assert reference["task_id"] and reference["context_id"] and reference["depth"] == 1
        remote = current["result"]["remote_result"]
        assert remote["operation_id"] != current["id"]
        assert remote["evidence_ids"] and current["evidence_ids"]
        await port.close()
        service.ports.pop(port.body_id)
