"""Readiness is a fresh challenge; command uncertainty never licenses replay."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from entryplug_harbor.mqtt_control import MqttFixtureControl
from entryplug_harbor.mqtt_light import AppliedLampState, LampTopics, MqttFixtureLamp


def test_retained_read_challenges_do_not_reach_world_owner() -> None:
    lamp = MqttFixtureLamp(LampTopics("probe-test"), "broker")
    message = SimpleNamespace(topic=lamp.topics.read_request, payload=b"a" * 32, retain=True)
    lamp._on_message(lamp._client, None, message)
    assert lamp._reads.empty()
    message.retain = False
    lamp._on_message(lamp._client, None, message)
    lamp._on_message(lamp._client, None, message)
    assert lamp._reads.qsize() == 1
    published = []
    lamp._publish = lambda topic, payload, *, retain: published.append(
        (topic, json.loads(payload), retain)
    )
    assert not lamp.process_one(
        lambda _: pytest.fail("read cannot apply"), lambda: AppliedLampState(0.5, 7, 1.2)
    )
    assert published == [
        (
            lamp.topics.read_reply,
            {"nonce": "a" * 32, "level": 0.5, "revision": 7, "sim_time_s": 1.2},
            False,
        )
    ]


def test_retained_or_wrong_topic_reply_cannot_establish_readiness() -> None:
    control = MqttFixtureControl(LampTopics("probe-test"), "broker")
    for topic, retained in ((control.topics.read_reply, True), (control.topics.state, False)):
        control._on_message(
            None, None, SimpleNamespace(topic=topic, retain=retained, payload=b'{"nonce":"test"}')
        )
    assert control._responses.empty()


@pytest.mark.asyncio
async def test_read_ignores_old_nonce_and_requires_current_response() -> None:
    control = MqttFixtureControl(LampTopics("probe-test"), "broker")

    def publish(topic: str, payload: bytes) -> None:
        assert topic == control.topics.read_request
        control._responses.put_nowait(
            {"nonce": "old", "level": 0.75, "revision": 50, "sim_time_s": 100}
        )
        control._responses.put_nowait(
            {"nonce": payload.decode(), "level": 0.25, "revision": 2, "sim_time_s": 1}
        )

    control._publish = publish
    assert await control.read_state() == AppliedLampState(0.25, 2, 1)
    assert len(control.reads) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key,value",
    [
        ("level", True),
        ("level", float("nan")),
        ("level", 0.9),
        ("revision", True),
        ("revision", -1),
        ("sim_time_s", None),
    ],
)
async def test_malformed_applied_reply_is_refused(key: str, value: object) -> None:
    control = MqttFixtureControl(LampTopics("probe-test"), "broker")

    def publish(_topic: str, payload: bytes) -> None:
        reply = {"nonce": payload.decode(), "level": 0.25, "revision": 2, "sim_time_s": 1}
        reply[key] = value
        control._responses.put_nowait(reply)

    control._publish = publish
    with pytest.raises(ValueError, match="invalid MQTT"):
        await control.read_state()


@pytest.mark.asyncio
async def test_applied_write_waits_for_progress_and_never_republishes_command() -> None:
    control = MqttFixtureControl(LampTopics("probe-test"), "broker")
    states = iter(
        [AppliedLampState(0, 1, 1), AppliedLampState(0, 1, 1), AppliedLampState(64 / 255, 2, 2)]
    )

    async def read(_timeout: float) -> AppliedLampState:
        return next(states)

    control._read = read
    sent = []
    control._publish = lambda topic, payload: sent.append((topic, json.loads(payload)))
    report = await control.set_brightness(0.25)
    assert report.available and report.level == 64 / 255
    assert sent == [(control.topics.command, {"state": "ON", "brightness": 64})]
    assert control.applied == [AppliedLampState(64 / 255, 2, 2)]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [TimeoutError, asyncio.CancelledError])
async def test_lost_reply_or_cancellation_never_replays_physical_command(
    failure: type[BaseException],
) -> None:
    control = MqttFixtureControl(LampTopics("probe-test"), "broker")
    count = 0

    async def read(_timeout: float) -> AppliedLampState:
        nonlocal count
        count += 1
        if count > 1:
            raise failure()
        return AppliedLampState(0, 1, 1)

    control._read = read
    sent = []
    control._publish = lambda topic, payload: sent.append(topic)
    with pytest.raises(failure):
        await control.set_brightness(0.5)
    assert sent == [control.topics.command]
    assert control.applied == []
