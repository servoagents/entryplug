"""Native Zenoh publications and actions through resident agent authority."""

import asyncio
import json
import socket

import pytest
import zenoh

from entryplug_app.borrowed_session import BorrowedSession
from entryplug_app.connections import Connections
from entryplug_app.service import ApplicationService
from entryplug_app.workspace import Workspace


@pytest.mark.asyncio
async def test_native_zenoh_agent_observes_approves_acts_and_refuses_lost_reply(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        endpoint = f"tcp/127.0.0.1:{sock.getsockname()[1]}"
    config = zenoh.Config.from_json5(
        json.dumps(
            {
                "mode": "peer",
                "listen": {"endpoints": [endpoint]},
                "scouting": {"multicast": {"enabled": False}},
            }
        )
    )
    service = ApplicationService(Workspace.resolve(str(tmp_path)), demo=False)
    await service.open()
    writes, drop = [], [False]
    with zenoh.open(config) as device:

        def command(query):
            request = json.loads(query.payload.to_bytes())
            writes.append(request)
            if not drop[0]:
                query.reply(
                    str(query.key_expr),
                    json.dumps(
                        {
                            "request_id": request["request_id"],
                            "status": "applied",
                            "result": {"level": request["arguments"]["level"], "simulated": True},
                        }
                    ),
                )

        handler = device.declare_queryable("lamp/instance-1/set", command)
        try:
            connections = Connections(service)
            connection = await connections.create(
                {
                    "kind": "zenoh",
                    "name": "Native room",
                    "url": endpoint,
                    "profile": {
                        "observations": [
                            {
                                "name": "room.temperature",
                                "key": "room/temperature",
                                "description": "Temperature Celsius",
                            }
                        ],
                        "actions": [
                            {
                                "name": "lamp.set",
                                "key": "lamp/instance-1/set",
                                "description": "Absolute brightness",
                                "timeout_s": 0.5,
                                "input_schema": {
                                    "type": "object",
                                    "properties": {
                                        "level": {"type": "number", "minimum": 0, "maximum": 0.75}
                                    },
                                    "required": ["level"],
                                    "additionalProperties": False,
                                },
                            }
                        ],
                    },
                }
            )
            async with asyncio.timeout(10):
                while connection["id"] not in service.ports:
                    state = await service.store.get("connections", connection["id"])
                    assert state["status"] != "unverified", state
                    await asyncio.sleep(0.01)
            grant = await service.command(
                "attachment.create",
                {
                    "body_id": connection["id"],
                    "name": "Native agent",
                    "allow": ["room.temperature", "lamp.set"],
                },
                "grant",
            )
            agent = BorrowedSession(service, grant["id"])
            assert next(c for c in (await agent.observe()).capabilities if c["name"] == "lamp.set")[
                "approval_required"
            ]

            async def completed(op):
                async with asyncio.timeout(5):
                    while not op.terminal:
                        op = await agent.wait(op, 0.05)
                    return op

            async with asyncio.timeout(5):
                while True:
                    device.put("room/temperature", '{"celsius":22,"simulated":true}')
                    await asyncio.sleep(0.03)
                    read = await completed(await agent.act("room.temperature", {}))
                    if read.lifecycle == "succeeded":
                        break
            op = await agent.act("lamp.set", {"level": 0.5}, request_id="same")
            assert writes == []
            stored = await service.store.get("operations", op.operation_id)
            assert stored["dispatch"] == "waiting_approval"
            await service.command(
                "approval.decision",
                {"approval_id": stored["approval_id"], "allow": True},
                "approve",
            )
            result = await completed(op)
            assert result.lifecycle == "succeeded", result
            assert result.effect_state == "reported"
            assert (
                await agent.act("lamp.set", {"level": 0.5}, request_id="same")
            ).operation_id == op.operation_id
            assert len(writes) == 1
            drop[0] = True
            op = await agent.act("lamp.set", {"level": 0.25})
            stored = await service.store.get("operations", op.operation_id)
            await service.command(
                "approval.decision",
                {"approval_id": stored["approval_id"], "allow": True},
                "approve-lost",
            )
            result = await completed(op)
            assert result.lifecycle == "indeterminate" and result.effect_state == "unknown"
            from entryplug.core.operation import AdmissionError

            with pytest.raises(AdmissionError):
                await agent.act("lamp.set", {"level": 0.5})
            assert len(writes) == 2
            assert (await service.store.get("operations", op.operation_id))["evidence_ids"]
        finally:
            handler.undeclare()
            await service.close()


@pytest.mark.asyncio
async def test_native_zenoh_acl_still_denies_an_owner_configured_action():
    from pathlib import Path

    from entryplug_app.network_port import network_port

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        endpoint = f"tcp/127.0.0.1:{sock.getsockname()[1]}"
    config = json.loads((Path(__file__).parents[2] / "examples/network-zenoh.json").read_text())
    config.update(id="acl-fixture", url=endpoint)
    config["profile"]["actions"][0]["timeout_s"] = 0.3
    key = config["profile"]["actions"][0]["key"]
    device_config = zenoh.Config.from_json5(
        json.dumps(
            {
                "mode": "peer",
                "listen": {"endpoints": [endpoint]},
                "scouting": {"multicast": {"enabled": False}},
                "access_control": {
                    "enabled": True,
                    "default_permission": "allow",
                    "rules": [
                        {
                            "id": "no-actions",
                            "permission": "deny",
                            "flows": ["ingress"],
                            "messages": ["query"],
                            "key_exprs": [key],
                        }
                    ],
                    "subjects": [{"id": "clients"}],
                    "policies": [{"rules": ["no-actions"], "subjects": ["clients"]}],
                },
            }
        )
    )
    received = []
    with zenoh.open(device_config) as device:
        handler = device.declare_queryable(key, lambda query: received.append(str(query.key_expr)))
        port = await network_port(config, None)
        try:
            op = await port.session.act("lamp.set", {"level": 0.5})
            async with asyncio.timeout(3):
                while not op.terminal:
                    op = await port.session.wait(op, 0.05)
            assert op.lifecycle == "indeterminate" and op.effect_state == "unknown"
            assert received == []
        finally:
            await port.close()
            handler.undeclare()
