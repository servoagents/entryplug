"""MQTT 5 wire behavior, using an explicitly supplied disposable test broker."""

import asyncio
import json
import os
import uuid
from urllib.parse import urlsplit

import pytest
from paho.mqtt import client as mqtt
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties

from entryplug.core.operation import AdmissionError
from entryplug_app.borrowed_session import BorrowedSession
from entryplug_app.connections import Connections
from entryplug_app.service import ApplicationService
from entryplug_app.workspace import Workspace


@pytest.mark.asyncio
async def test_mqtt5_native_agent_scope_correlation_retained_and_unknown_effects(tmp_path):
    endpoint = os.environ.get("ENTRYPLUG_TEST_MQTT_URL")
    if not endpoint:
        pytest.skip("Set ENTRYPLUG_TEST_MQTT_URL to an owned disposable MQTT 5 broker")
    parsed = urlsplit(endpoint)
    prefix = "test/" + uuid.uuid4().hex
    state_key, action_key, reply_key = [prefix + "/" + s for s in ("state", "set", "reply")]
    loop = asyncio.get_running_loop()
    ready = asyncio.Event()
    device = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv5)
    requests, mode = [], ["applied"]

    def connect(client, userdata, flags, reason, properties):
        client.subscribe(action_key)

    def message(client, userdata, message):
        request = json.loads(message.payload)
        requests.append(request)
        assert not message.retain and message.qos == 0
        assert message.properties.ResponseTopic == reply_key
        assert message.properties.CorrelationData == request["request_id"].encode()
        properties = Properties(PacketTypes.PUBLISH)
        properties.CorrelationData = message.properties.CorrelationData
        response = json.dumps(
            {
                "request_id": request["request_id"],
                "status": "applied",
                "result": {"simulated": True, "level": 0.5},
            }
        )
        if mode[0] == "applied":
            client.publish(reply_key, response, properties=properties)
        elif mode[0] == "wrong":
            properties.CorrelationData = b"old-operation"
            client.publish(reply_key, response, properties=properties)

    device.on_connect = connect
    device.on_subscribe = lambda *args: loop.call_soon_threadsafe(ready.set)
    device.on_message = message
    device.connect(parsed.hostname, parsed.port or 1883)
    device.loop_start()
    service = ApplicationService(Workspace.resolve(str(tmp_path)), demo=False)
    await service.open()
    try:
        await asyncio.wait_for(ready.wait(), 5)
        receipt = device.publish(state_key, '{"celsius":20}', qos=1, retain=True)
        await asyncio.to_thread(receipt.wait_for_publish, 3)
        connections = Connections(service)
        connection = await connections.create(
            {
                "kind": "mqtt",
                "name": "Native MQTT room",
                "url": endpoint,
                "profile": {
                    "reply_key": reply_key,
                    "observations": [
                        {
                            "name": "room.temperature",
                            "key": state_key,
                            "description": "Room temperature Celsius",
                        }
                    ],
                    "actions": [
                        {
                            "name": "lamp.set",
                            "key": action_key,
                            "description": "Absolute brightness",
                            "timeout_s": 0.3,
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
                current = await service.store.get("connections", connection["id"])
                assert current["status"] != "unverified", current
                await asyncio.sleep(0.01)
        grant = await service.command(
            "attachment.create",
            {
                "name": "Delegated MQTT agent",
                "body_id": connection["id"],
                "allow": ["room.temperature", "lamp.set"],
                "approve": [],
            },
            "grant",
        )
        observer_grant = await service.command(
            "attachment.create",
            {
                "name": "Observer",
                "body_id": connection["id"],
                "allow": ["room.temperature"],
            },
            "observer",
        )
        agent, observer = (
            BorrowedSession(service, grant["id"]),
            BorrowedSession(service, observer_grant["id"]),
        )

        async def complete(op):
            async with asyncio.timeout(5):
                while not op.terminal:
                    op = await agent.wait(op, 0.05)
                return op

        with pytest.raises(AdmissionError):
            await observer.act("lamp.set", {"level": 0.5})
        assert requests == []
        async with asyncio.timeout(3):
            while True:
                retained = await complete(await agent.act("room.temperature", {}))
                if retained.reason_code != "NO_SAMPLE":
                    break
                await asyncio.sleep(0.01)
        assert retained.lifecycle == "failed"
        assert retained.reason_code == "RETAINED_UNVERIFIED"
        async with asyncio.timeout(3):
            while True:
                device.publish(state_key, '{"celsius":21}')
                await asyncio.sleep(0.02)
                if (
                    await complete(await agent.act("room.temperature", {}))
                ).lifecycle == "succeeded":
                    break
        op = await agent.act("lamp.set", {"level": 0.5}, request_id="one-command")
        result = await complete(op)
        assert result.lifecycle == "succeeded", result
        assert result.effect_state == "reported"
        assert (
            await agent.act("lamp.set", {"level": 0.5}, request_id="one-command")
        ).operation_id == op.operation_id
        assert len(requests) == 1
        mode[0] = "wrong"
        result = await complete(await agent.act("lamp.set", {"level": 0.25}))
        assert result.lifecycle == "indeterminate" and result.effect_state == "unknown"
        with pytest.raises(AdmissionError):
            await agent.act("lamp.set", {"level": 0.5})
        # A new transport/runtime cannot erase the durable uncertainty for this body.
        await connections.disconnect(connection["id"])
        await connections.retry(connection["id"])
        async with asyncio.timeout(5):
            while connection["id"] not in service.ports:
                await asyncio.sleep(0.01)
        with pytest.raises(AdmissionError):
            await agent.act("lamp.set", {"level": 0.5})
        assert len(requests) == 2
    finally:
        await service.close()
        device.publish(state_key, b"", retain=True)
        device.disconnect()
        await asyncio.to_thread(device.loop_stop)


@pytest.mark.asyncio
async def test_native_mqtt_acl_still_denies_an_owner_configured_action():
    from entryplug_app.network_port import network_port

    endpoint = os.environ.get("ENTRYPLUG_TEST_MQTT_URL")
    prefix = os.environ.get("ENTRYPLUG_TEST_MQTT_DENIED_PREFIX")
    if not endpoint or not prefix:
        pytest.skip("Requires a disposable broker with read-only ACL for the denied prefix")
    parsed = urlsplit(endpoint)
    key = prefix + "/" + uuid.uuid4().hex + "/set"
    listener = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv5)
    loop, ready, received = asyncio.get_running_loop(), asyncio.Event(), []
    listener.on_connect = lambda client, *args: client.subscribe(key)
    listener.on_subscribe = lambda *args: loop.call_soon_threadsafe(ready.set)
    listener.on_message = lambda client, userdata, message: received.append(message.payload)
    listener.connect(parsed.hostname, parsed.port or 1883)
    listener.loop_start()
    try:
        await asyncio.wait_for(ready.wait(), 5)
        port = await network_port(
            {
                "id": "mqtt-acl-fixture",
                "name": "Denied MQTT action",
                "kind": "mqtt",
                "url": endpoint,
                "profile": {
                    "reply_key": key + "/reply",
                    "actions": [
                        {
                            "name": "device.act",
                            "key": key,
                            "description": "ACL refusal fixture",
                            "timeout_s": 0.3,
                            "input_schema": {
                                "type": "object",
                                "properties": {},
                                "additionalProperties": False,
                            },
                        }
                    ],
                },
            },
            None,
        )
        try:
            op = await port.session.act("device.act", {})
            async with asyncio.timeout(3):
                while not op.terminal:
                    op = await port.session.wait(op, 0.05)
            assert op.lifecycle == "indeterminate" and op.effect_state == "unknown"
            assert received == []
        finally:
            await port.close()
    finally:
        listener.disconnect()
        await asyncio.to_thread(listener.loop_stop)
