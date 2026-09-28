"""MQTT JSON-light fixture: applied revisions, not command acknowledgments."""

from __future__ import annotations

import json
import os
import time
import uuid
from types import SimpleNamespace

import pytest
from paho.mqtt import client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

from entryplug_harbor.mqtt_light import (
    AppliedLampState,
    LampTopics,
    MqttFixtureLamp,
    decode_absolute_level,
)


def test_discovery_is_run_scoped_and_nonoptimistic() -> None:
    topics = LampTopics("run-1")
    config = json.loads(topics.discovery_payload())
    assert config["schema"] == "json"
    assert config["optimistic"] is False
    assert config["supported_color_modes"] == ["brightness"]
    assert config["command_topic"] == "entryplug/fixture/run-1/lamp/set"
    assert config["state_topic"] == "entryplug/fixture/run-1/lamp/state"
    assert topics.entity_id == "light.entryplug_run_1"
    with pytest.raises(ValueError):
        LampTopics("run/#")


@pytest.mark.parametrize(
    "payload",
    [
        b'{"state":"ON","brightness":true}',
        b'{"state":"ON","brightness":255}',
        b'{"state":"ON","brightness":192}',
        b'{"state":"ON"}',
        b'{"state":"TOGGLE"}',
        b'{"state":"OFF","effect":"flash"}',
        b"not-json",
    ],
)
def test_only_bounded_absolute_commands_are_accepted(payload: bytes) -> None:
    with pytest.raises(ValueError):
        decode_absolute_level(payload)
    assert decode_absolute_level(b'{"state":"OFF"}') == 0
    assert decode_absolute_level(b'{"state":"ON","brightness":191}') == pytest.approx(191 / 255)
    with pytest.raises(ValueError):
        decode_absolute_level(b'{"state":"ON","brightness":191}', retained=True)


def test_callback_queues_one_request_and_state_waits_for_world_apply() -> None:
    lamp = MqttFixtureLamp(LampTopics("run-2"), "127.0.0.1")
    published: list[tuple[str, bytes | str, bool]] = []
    lamp._publish = lambda topic, payload, *, retain: published.append((topic, payload, retain))
    message = SimpleNamespace(
        topic=lamp.topics.command, payload=b'{"state":"ON","brightness":191}', retain=False
    )
    lamp._on_message(lamp._client, None, message)
    lamp._on_message(lamp._client, None, message)
    assert lamp.rejected_commands == 1
    assert published == []
    applied: list[float] = []

    def world_apply(level: float) -> AppliedLampState:
        applied.append(level)
        return AppliedLampState(level, 1)

    assert lamp.process_one(world_apply)
    assert applied == [pytest.approx(191 / 255)]
    assert json.loads(published[0][1]) == {"state": "ON", "brightness": 191}
    assert published[0][2] is True
    assert not lamp.process_one(world_apply)


def test_unconfirmed_world_apply_never_publishes_state() -> None:
    lamp = MqttFixtureLamp(LampTopics("run-3"), "127.0.0.1")
    published: list[object] = []
    lamp._publish = lambda *args, **kwargs: published.append((args, kwargs))
    message = SimpleNamespace(
        topic=lamp.topics.command, payload=b'{"state":"ON","brightness":191}', retain=False
    )
    lamp._on_message(lamp._client, None, message)
    with pytest.raises(ValueError, match="did not confirm"):
        lamp.process_one(lambda _: AppliedLampState(0.0, 1))
    assert published == []


def test_real_mosquitto_delivers_command_and_only_applied_state() -> None:
    port_text = os.environ.get("ENTRYPLUG_MQTT_TEST_PORT")
    if port_text is None:
        pytest.skip("explicit local Mosquitto test port was not provided")
    port = int(port_text)
    topics = LampTopics(f"test-{uuid.uuid4().hex[:12]}")
    seen: list[tuple[str, bytes]] = []
    listener = mqtt.Client(CallbackAPIVersion.VERSION2, client_id=f"listener-{uuid.uuid4().hex}")
    listener.on_message = lambda _client, _userdata, message: seen.append(
        (message.topic, message.payload)
    )
    listener.connect("127.0.0.1", port)
    listener.loop_start()
    listener.subscribe([(topics.state, 0), (topics.discovery, 0)])
    lamp = MqttFixtureLamp(topics, "127.0.0.1", port)
    try:
        lamp.start()
        lamp.publish_initial_state(AppliedLampState(0.0, 0))
        command = mqtt.Client(CallbackAPIVersion.VERSION2, client_id=f"command-{uuid.uuid4().hex}")
        try:
            command.connect("127.0.0.1", port)
            command.loop_start()
            receipt = command.publish(
                topics.command, b'{"state":"ON","brightness":191}', qos=0, retain=False
            )
            receipt.wait_for_publish(timeout=3.0)
            deadline = time.monotonic() + 3.0
            while lamp._pending.qsize() == 0 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert lamp._pending.qsize() == 1
            assert not any(
                topic == topics.state and json.loads(payload).get("state") == "ON"
                for topic, payload in seen
            )
            assert lamp.process_one(lambda level: AppliedLampState(level, 1))
            while time.monotonic() < deadline and not any(
                topic == topics.state and json.loads(payload).get("state") == "ON"
                for topic, payload in seen
            ):
                time.sleep(0.01)
            assert any(
                topic == topics.state and json.loads(payload).get("brightness") == 191
                for topic, payload in seen
            )
            assert any(
                topic == topics.discovery and json.loads(payload).get("optimistic") is False
                for topic, payload in seen
                if payload
            )
        finally:
            command.disconnect()
            command.loop_stop()
    finally:
        lamp.close()
        listener.disconnect()
        listener.loop_stop()


def test_failed_subscription_never_marks_fixture_ready() -> None:
    lamp = MqttFixtureLamp(LampTopics("run-4"), "127.0.0.1")
    lamp._on_subscribe(lamp._client, None, 1, [SimpleNamespace(is_failure=True)], None)
    assert not lamp._ready.is_set()
