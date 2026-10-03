"""Single-writer MQTT fixture control; fresh read challenges, absolute writes.

This port targets the owned fixture protocol, not arbitrary MQTT lights. A
readback is a device report; it does not establish independent physical truth.
Commands are sent once at QoS 0. Read-only challenges may be repeated while
waiting for a new applied revision, never the physical command itself.
"""

from __future__ import annotations

import asyncio
import json
import math
import queue
import threading
import time
import uuid

from paho.mqtt import client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

from entryplug.embodiment.inspection import LightReport
from entryplug_harbor.mqtt_light import AppliedLampState, LampTopics


class MqttFixtureControl:
    def __init__(self, topics: LampTopics, host: str, port: int = 1883) -> None:
        self.topics = topics
        self.entity_id = topics.entity_id
        self._host, self._port = host, port
        self._ready = threading.Event()
        self._responses: queue.Queue[dict[str, object]] = queue.Queue(maxsize=8)
        self._lock = asyncio.Lock()
        self._client = mqtt.Client(
            CallbackAPIVersion.VERSION2,
            client_id=f"control-{uuid.uuid4().hex}",
            reconnect_on_failure=False,
        )
        self._client.on_connect = self._on_connect
        self._client.on_subscribe = self._on_subscribe
        self._client.on_message = self._on_message
        self._started = False
        self.applied: list[AppliedLampState] = []
        self.reads: list[dict[str, object]] = []
        self.commands: list[dict[str, object]] = []

    def _on_connect(
        self,
        client: mqtt.Client,
        _userdata: object,
        _flags: object,
        reason: object,
        _properties: object,
    ) -> None:
        if reason == 0:
            client.subscribe(self.topics.read_reply, qos=0)

    def _on_subscribe(
        self,
        _client: mqtt.Client,
        _userdata: object,
        _mid: int,
        reasons: object,
        _properties: object,
    ) -> None:
        if (
            isinstance(reasons, list)
            and reasons
            and all(not getattr(r, "is_failure", True) for r in reasons)
        ):
            self._ready.set()

    def _on_message(
        self,
        _client: mqtt.Client,
        _userdata: object,
        message: mqtt.MQTTMessage,
    ) -> None:
        if message.topic != self.topics.read_reply or message.retain or len(message.payload) > 512:
            return
        try:
            data = json.loads(message.payload)
            if isinstance(data, dict):
                self._responses.put_nowait(data)
        except (ValueError, queue.Full):
            pass

    def start(self) -> None:
        if self._started:
            raise RuntimeError("MQTT fixture control already started")
        self._client.connect(self._host, self._port, keepalive=15)
        self._client.loop_start()
        self._started = True
        if not self._ready.wait(5):
            self.close()
            raise TimeoutError("MQTT readback subscription not ready")

    def close(self) -> None:
        if self._started:
            self._client.disconnect()
            self._client.loop_stop()
            self._started = False

    def _publish(self, topic: str, payload: bytes) -> None:
        receipt = self._client.publish(topic, payload, qos=0, retain=False)
        if receipt.rc != mqtt.MQTT_ERR_SUCCESS:
            raise TimeoutError("MQTT fixture publish was not sent")

    async def _read(self, timeout: float) -> AppliedLampState:
        nonce = uuid.uuid4().hex
        started = time.monotonic()
        deadline = started + timeout
        self._publish(self.topics.read_request, nonce.encode())
        while time.monotonic() < deadline:
            try:
                data = self._responses.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.01)
                continue
            if data.get("nonce") != nonce:
                continue
            level, revision, stamp = data.get("level"), data.get("revision"), data.get("sim_time_s")
            if (
                not isinstance(level, (int, float))
                or isinstance(level, bool)
                or not math.isfinite(level)
                or not 0 <= level <= 0.75
                or type(revision) is not int
                or revision < 0
                or not isinstance(stamp, (int, float))
                or isinstance(stamp, bool)
                or not math.isfinite(stamp)
                or stamp < 0
            ):
                raise ValueError("invalid MQTT applied state")
            self.reads.append(
                {**data, "requested_monotonic": started, "received_monotonic": time.monotonic()}
            )
            return AppliedLampState(float(level), revision, float(stamp))
        raise TimeoutError("no current MQTT bridge reply")

    async def read_state(self) -> AppliedLampState:
        async with self._lock:
            return await self._read(1.0)

    async def set_brightness(self, level: float) -> LightReport:
        if (
            not isinstance(level, (int, float))
            or isinstance(level, bool)
            or not math.isfinite(level)
            or not 0 <= level <= 0.75
        ):
            raise ValueError("brightness outside fixture bounds")
        async with self._lock:
            before = await self._read(1.0)
            brightness = min(191, round(level * 255))
            payload = {"state": "ON", "brightness": brightness} if brightness else {"state": "OFF"}
            self.commands.append(
                {
                    "payload": payload,
                    "prior_revision": before.revision,
                    "sent_monotonic": time.monotonic(),
                    "retain": False,
                    "qos": 0,
                }
            )
            self._publish(self.topics.command, json.dumps(payload).encode())
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                state = await self._read(min(1.0, deadline - time.monotonic()))
                if state.revision > before.revision:
                    if abs(state.level - level) > 1 / 255:
                        raise RuntimeError("interfering MQTT lamp revision")
                    self.applied.append(state)
                    return LightReport(self.entity_id, state.level, True)
                await asyncio.sleep(0.05)
            raise TimeoutError("MQTT lamp application not confirmed")
