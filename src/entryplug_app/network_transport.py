"""Optional native SDK transports. No agent, grant, or physical policy lives here."""

from __future__ import annotations

import asyncio
import json
import math
import threading
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

MAX_PAYLOAD = 8192
Receive = Callable[..., None]


class MqttTransport:
    """MQTT 5 request/response; fresh session, non-retained QoS 0 commands, no replay."""

    def __init__(self, config: dict[str, Any], credentials: str | None, receive: Receive):
        from paho.mqtt import client as mqtt
        from paho.mqtt.enums import CallbackAPIVersion

        self.loop = asyncio.get_running_loop()
        self.ready: asyncio.Future[None] = self.loop.create_future()
        self.pending: tuple[bytes, asyncio.Future[bytes]] | None = None
        self.lock = threading.Lock()
        self.reply_queued = False
        self.available = False
        self.closed = False
        self.receive = receive
        self.config = config
        self.reply_key = config["profile"].get("reply_key")
        self.client = mqtt.Client(
            CallbackAPIVersion.VERSION2,
            client_id=config["id"],
            protocol=mqtt.MQTTv5,
            reconnect_on_failure=False,
        )
        self.client.max_queued_messages_set(1)
        self.client.connect_timeout = 5
        auth = json.loads(credentials) if credentials else {}
        if not isinstance(auth, dict) or set(auth) - {
            "username",
            "password",
            "ca_file",
            "cert_file",
            "key_file",
        }:
            raise ValueError(
                "MQTT credentials must contain only username/password and TLS file paths"
            )
        if any(not isinstance(value, str) for value in auth.values()):
            raise ValueError("MQTT credential fields must be strings")
        if "password" in auth and "username" not in auth:
            raise ValueError("MQTT password requires a username")
        endpoint = urlsplit(config["url"])
        if "username" in auth:
            self.client.username_pw_set(auth["username"], auth.get("password"))
        if endpoint.scheme == "mqtts":
            self.client.tls_set(
                ca_certs=auth.get("ca_file"),
                certfile=auth.get("cert_file"),
                keyfile=auth.get("key_file"),
            )
        elif any(key in auth for key in ("ca_file", "cert_file", "key_file")):
            raise ValueError("TLS credentials require mqtts://")
        self.client.on_connect = self._connect
        self.client.on_subscribe = self._subscribe
        self.client.on_disconnect = self._disconnect
        self.client.on_message = self._message
        self.endpoint = endpoint

    def _settle_ready(self, error: str | None = None) -> None:
        if not self.ready.done():
            if error:
                self.ready.set_exception(ConnectionError(error))
            else:
                self.ready.set_result(None)

    def _connect(
        self, client: Any, userdata: Any, flags: Any, reason: Any, properties: Any
    ) -> None:
        if reason.is_failure:
            self.loop.call_soon_threadsafe(
                self._settle_ready, "MQTT authentication/connect refused"
            )
            return
        keys = [item["key"] for item in self.config["profile"].get("observations", [])]
        if self.reply_key:
            keys.append(self.reply_key)
        # Every usable profile has at least one observation or a response topic.
        rc, _ = client.subscribe([(key, 0) for key in keys])
        if rc != 0:
            self.loop.call_soon_threadsafe(self._settle_ready, "MQTT subscribe failed")

    def _subscribe(
        self, client: Any, userdata: Any, mid: Any, reasons: Any, properties: Any
    ) -> None:
        if not reasons or any(r.is_failure for r in reasons):
            self.loop.call_soon_threadsafe(self._settle_ready, "MQTT subscription denied")
            return
        self.available = True
        self.loop.call_soon_threadsafe(self._settle_ready)

    def _disconnect(self, *args: Any) -> None:
        self.available = False
        self.loop.call_soon_threadsafe(self._settle_ready, "MQTT disconnected")
        self.loop.call_soon_threadsafe(self._fail_pending)

    def _fail_pending(self) -> None:
        with self.lock:
            pending = self.pending
        if pending and not pending[1].done():
            pending[1].set_exception(ConnectionError("MQTT disconnected"))

    def _message(self, client: Any, userdata: Any, message: Any) -> None:
        if self.closed:
            return
        if message.topic != self.reply_key:
            self.receive(message.topic, message.payload, retained=bool(message.retain))
            return
        correlation = getattr(message.properties, "CorrelationData", None)
        with self.lock:
            pending = self.pending
            if not pending or message.retain or correlation != pending[0] or self.reply_queued:
                return
            self.reply_queued = True
        # Bound the data before crossing threads; an oversized reply remains a failed reply.
        payload = message.payload if len(message.payload) <= MAX_PAYLOAD else b""

        def deliver() -> None:
            if not pending[1].done():
                pending[1].set_result(payload)

        self.loop.call_soon_threadsafe(deliver)

    async def open(self) -> None:
        # A canceled connect must be drained before closing the client it can still mutate.
        task = asyncio.create_task(
            asyncio.to_thread(
                self.client.connect,
                self.endpoint.hostname,
                self.endpoint.port or (8883 if self.endpoint.scheme == "mqtts" else 1883),
                15,
            )
        )
        try:
            await asyncio.shield(task)
            self.client.loop_start()
            async with asyncio.timeout(6):
                await self.ready
        except BaseException:
            await asyncio.gather(task, return_exceptions=True)
            await self.close()
            raise

    def check(self) -> None:
        if self.closed or not self.available or not self.client.is_connected():
            raise ConnectionError("MQTT source is disconnected")

    async def request(self, key: str, payload: bytes, request_id: str, timeout: float) -> bytes:
        from paho.mqtt.packettypes import PacketTypes
        from paho.mqtt.properties import Properties

        self.check()
        future: asyncio.Future[bytes] = self.loop.create_future()
        correlation = request_id.encode("ascii")
        with self.lock:
            if self.pending is not None:
                raise RuntimeError("MQTT action already pending")
            self.pending, self.reply_queued = (correlation, future), False
        try:
            properties = Properties(PacketTypes.PUBLISH)  # type: ignore[no-untyped-call]
            properties.ResponseTopic = self.reply_key
            properties.CorrelationData = correlation
            properties.MessageExpiryInterval = max(1, math.ceil(timeout))
            properties.ContentType = "application/json"
            # QoS 0 and no reconnect prevent SDK retransmission of an uncertain command.
            receipt = self.client.publish(key, payload, qos=0, retain=False, properties=properties)
            if receipt.rc != 0:
                raise ConnectionError("MQTT command delivery unconfirmed")
            async with asyncio.timeout(timeout):
                return await future
        finally:
            with self.lock:
                self.pending = None
            future.cancel()

    async def close(self) -> None:
        self.closed, self.available = True, False
        self._fail_pending()
        self.client.disconnect()
        await asyncio.to_thread(self.client.loop_stop)


class ZenohTransport:
    """Exact native keys and bounded query/reply; no RMW key-layout assumptions."""

    def __init__(self, config: dict[str, Any], credentials: str | None, receive: Receive):
        self.config, self.credentials, self.receive = config, credentials, receive
        self.session: Any = None
        self.subscribers: list[Any] = []

    async def open(self) -> None:
        import zenoh

        # The environment value is a native JSON5 config (TLS/auth/ACL), held only in memory.
        config = zenoh.Config.from_json5(self.credentials or "{}")
        config.insert_json5("mode", '"client"')
        config.insert_json5("connect/endpoints", json.dumps([self.config["url"]]))
        config.insert_json5("scouting/multicast/enabled", "false")
        config.insert_json5("scouting/gossip/enabled", "false")
        config.insert_json5("connect/timeout_ms", "5000")

        def receive(sample: Any) -> None:
            self.receive(
                str(sample.key_expr),
                sample.payload.to_bytes(),
                deleted=sample.kind == zenoh.SampleKind.DELETE,
                source_timestamp=str(sample.timestamp) if sample.timestamp else None,
            )

        task = asyncio.create_task(asyncio.to_thread(zenoh.open, config))
        try:
            self.session = await asyncio.shield(task)
            for item in self.config["profile"].get("observations", []):
                self.subscribers.append(self.session.declare_subscriber(item["key"], receive))
            self.check()
        except BaseException:
            result = await asyncio.gather(task, return_exceptions=True)
            if result and not isinstance(result[0], BaseException):
                self.session = result[0]
            await self.close()
            raise

    def check(self) -> None:
        if not self.session or self.session.is_closed():
            raise ConnectionError("Zenoh session closed")
        if not self.session.info.routers_zid() and not self.session.info.peers_zid():
            raise ConnectionError("Zenoh endpoint unavailable")

    async def request(self, key: str, payload: bytes, request_id: str, timeout: float) -> bytes:
        import zenoh

        self.check()
        loop = asyncio.get_running_loop()
        finished: asyncio.Future[None] = loop.create_future()
        replies: list[bytes] = []
        lock = threading.Lock()
        cancel = zenoh.CancellationToken()

        def receive(reply: Any) -> None:
            sample = reply.ok
            data = sample.payload.to_bytes() if sample and str(sample.key_expr) == key else b""
            with lock:
                if len(replies) < 2:
                    replies.append(data if len(data) <= MAX_PAYLOAD else b"")

        def finish() -> None:
            if not finished.done():
                finished.set_result(None)

        try:
            self.session.get(
                key,
                zenoh.handlers.Callback(receive, lambda: loop.call_soon_threadsafe(finish)),
                payload=payload,
                encoding="application/json",
                timeout=timeout,
                target=zenoh.QueryTarget.ALL,
                consolidation=zenoh.ConsolidationMode.NONE,
                cancellation_token=cancel,
            )
            async with asyncio.timeout(timeout + 0.1):
                await finished
            with lock:
                if len(replies) != 1:
                    raise ValueError("Expected exactly one device reply")
                return replies[0]
        finally:
            # Retire local replies; this does not assert device-side cancellation.
            finished.cancel()
            cancel.cancel()  # type: ignore[no-untyped-call]  # SDK omits return annotation.

    async def close(self) -> None:
        for subscriber in self.subscribers:
            subscriber.undeclare()
        self.subscribers.clear()
        if self.session:
            await asyncio.to_thread(self.session.close)
            self.session = None
