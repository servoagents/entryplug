"""Run-scoped MQTT JSON-light fixture with applied-state-only reports.

This is fixture transport, not a general MQTT provider. Network callbacks
enqueue requests; the world-owning caller applies one request and only then
publishes the resulting device state.
"""

from __future__ import annotations

import json
import math
import queue
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass

from paho.mqtt import client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


@dataclass(frozen=True, slots=True)
class AppliedLampState:
    level: float
    revision: int
    sim_time_s: float | None = None


@dataclass(frozen=True, slots=True)
class LampTopics:
    run_id: str

    def __post_init__(self) -> None:
        if _RUN_ID.fullmatch(self.run_id) is None:
            raise ValueError("run ID must be one concrete MQTT topic segment")

    @property
    def base(self) -> str:
        return f"entryplug/fixture/{self.run_id}/lamp"

    @property
    def command(self) -> str:
        return f"{self.base}/set"

    @property
    def state(self) -> str:
        return f"{self.base}/state"

    @property
    def read_request(self) -> str:
        return f"{self.base}/read"

    @property
    def read_reply(self) -> str:
        return f"{self.base}/readback"

    @property
    def availability(self) -> str:
        return f"{self.base}/availability"

    @property
    def discovery(self) -> str:
        return f"homeassistant/light/entryplug_{self.run_id}/config"

    @property
    def entity_id(self) -> str:
        return f"light.entryplug_{self.run_id.replace('-', '_')}"

    def discovery_payload(self) -> bytes:
        return json.dumps(
            {
                "name": "Entryplug fixture lamp",
                "unique_id": f"entryplug_fixture_{self.run_id}",
                "default_entity_id": self.entity_id,
                "schema": "json",
                "command_topic": self.command,
                "state_topic": self.state,
                "availability_topic": self.availability,
                "supported_color_modes": ["brightness"],
                "brightness": True,
                "optimistic": False,
                "qos": 0,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()


def decode_absolute_level(payload: bytes, *, retained: bool = False) -> float:
    """Accept only non-retained absolute MQTT JSON-light brightness commands."""
    if retained or not payload or len(payload) > 256:
        raise ValueError("light command is retained or oversized")
    try:
        data = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("light command must be JSON") from error
    if not isinstance(data, dict):
        raise ValueError("light command must be an object")
    if data.get("state") == "OFF" and set(data) <= {"state", "brightness"}:
        return 0.0
    brightness = data.get("brightness")
    if (
        data.get("state") != "ON"
        or set(data) != {"state", "brightness"}
        or type(brightness) is not int
        or not 1 <= brightness <= 255
    ):
        raise ValueError("light command must be absolute ON brightness or OFF")
    level = brightness / 255
    if level > 0.75:
        raise ValueError("light command exceeds the fixture cap")
    return level


class MqttFixtureLamp:
    """One bounded MQTT ingress; the caller owns the ROS/world apply step."""

    def __init__(self, topics: LampTopics, broker_host: str, broker_port: int = 1883) -> None:
        if not broker_host or type(broker_port) is not int or not 1 <= broker_port <= 65535:
            raise ValueError("an explicit MQTT broker host and port are required")
        self.topics = topics
        self._broker_host = broker_host
        self._broker_port = broker_port
        self._pending: queue.Queue[float] = queue.Queue(maxsize=1)
        self._reads: queue.Queue[str] = queue.Queue(maxsize=1)
        self._last_state: AppliedLampState | None = None
        self._ready = threading.Event()
        self._last_revision = -1
        self.rejected_commands = 0
        self._client = mqtt.Client(
            CallbackAPIVersion.VERSION2,
            client_id=f"entryplug-fixture-{topics.run_id}",
            protocol=mqtt.MQTTv311,
            reconnect_on_failure=False,
        )
        self._client.on_connect = self._on_connect
        self._client.on_subscribe = self._on_subscribe
        self._client.on_message = self._on_message
        self._client.will_set(topics.availability, "offline", qos=1, retain=True)
        self._started = False

    def _on_connect(
        self,
        client: mqtt.Client,
        _userdata: object,
        _flags: object,
        reason: object,
        _properties: object,
    ) -> None:
        if reason == 0:
            client.subscribe([(self.topics.command, 0), (self.topics.read_request, 0)])

    def _on_subscribe(
        self,
        _client: mqtt.Client,
        _userdata: object,
        _mid: int,
        reasons: object,
        _properties: object,
    ) -> None:
        if not isinstance(reasons, list) or any(
            getattr(reason, "is_failure", True) for reason in reasons
        ):
            return
        self._ready.set()

    def _on_message(
        self, _client: mqtt.Client, _userdata: object, message: mqtt.MQTTMessage
    ) -> None:
        if message.topic == self.topics.read_request:
            # Read-only nonce challenges cannot be satisfied by retained state.
            if not message.retain and re.fullmatch(rb"[a-f0-9]{32}", message.payload):
                try:
                    self._reads.put_nowait(message.payload.decode("ascii"))
                except queue.Full:
                    pass
            return
        if message.topic != self.topics.command:
            return
        try:
            level = decode_absolute_level(message.payload, retained=message.retain)
            self._pending.put_nowait(level)
        except (ValueError, queue.Full):
            self.rejected_commands += 1

    def _publish(self, topic: str, payload: bytes | str, *, retain: bool) -> None:
        receipt = self._client.publish(topic, payload, qos=1, retain=retain)
        receipt.wait_for_publish(timeout=3.0)
        if not receipt.is_published():
            raise RuntimeError("MQTT publish was not confirmed")

    def start(self) -> None:
        if self._started:
            raise RuntimeError("fixture lamp is already started")
        self._client.connect(self._broker_host, self._broker_port, keepalive=15)
        self._client.loop_start()
        self._started = True
        if not self._ready.wait(5.0):
            self.close()
            raise TimeoutError("MQTT command subscription was not ready")
        self._publish(self.topics.discovery, self.topics.discovery_payload(), retain=True)
        self._publish(self.topics.availability, "online", retain=True)

    def publish_initial_state(self, state: AppliedLampState) -> None:
        if self._last_revision != -1:
            raise RuntimeError("initial lamp state was already reported")
        self._publish_applied(state)

    def _publish_applied(self, state: AppliedLampState) -> None:
        if (
            not isinstance(state, AppliedLampState)
            or not math.isfinite(state.level)
            or not 0 <= state.level <= 0.75
            or type(state.revision) is not int
            or state.revision <= self._last_revision
        ):
            raise ValueError("lamp state is not a new bounded applied revision")
        payload: dict[str, object] = {"state": "ON" if state.level > 0 else "OFF"}
        if state.level > 0:
            payload["brightness"] = round(state.level * 255)
        self._publish(
            self.topics.state,
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode(),
            retain=True,
        )
        self._last_revision = state.revision
        self._last_state = state

    def process_one(
        self,
        apply: Callable[[float], AppliedLampState],
        observe: Callable[[], AppliedLampState] | None = None,
    ) -> bool:
        """Answer bounded reads and apply at most one command in the world owner."""
        try:
            nonce = self._reads.get_nowait()
        except queue.Empty:
            pass
        else:
            state = observe() if observe is not None else self._last_state
            if state is not None:
                self._publish(
                    self.topics.read_reply,
                    json.dumps(
                        {
                            "nonce": nonce,
                            "level": state.level,
                            "revision": state.revision,
                            "sim_time_s": state.sim_time_s,
                        },
                        allow_nan=False,
                    ).encode(),
                    retain=False,
                )
        try:
            requested = self._pending.get_nowait()
        except queue.Empty:
            return False
        state = apply(requested)
        if not isinstance(state, AppliedLampState) or abs(state.level - requested) > 1 / 255:
            raise ValueError("world owner did not confirm the requested lamp level")
        self._publish_applied(state)
        return True

    def close(self) -> None:
        if not self._started:
            return
        try:
            self._publish(self.topics.availability, "offline", retain=True)
            self._publish(self.topics.discovery, b"", retain=True)
        finally:
            self._client.disconnect()
            self._client.loop_stop()
            self._started = False
