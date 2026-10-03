"""One explicitly allowed HA light over its authenticated WebSocket API.

The service result and subsequent HA state are device reports, not independent
evidence that the rendered target became visible. The inspection task performs
that check with fresh camera frames.
"""

from __future__ import annotations

import asyncio
import ipaddress
import math
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from entryplug.embodiment.inspection import LightReport

_BRIGHTNESS_MODES = frozenset(
    {"brightness", "color_temp", "hs", "rgb", "rgbw", "rgbww", "white", "xy"}
)


class HomeAssistantLightError(RuntimeError):
    """A failed HA exchange; a sent service action may still have taken effect."""


def _validate_url(url: str, *, allow_insecure_network: bool) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"ws", "wss"} or parsed.path != "/api/websocket":
        raise ValueError("expected an explicit ws(s)://host/api/websocket URL")
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("WebSocket URL must not contain credentials, query or fragment")
    if parsed.scheme == "wss" or allow_insecure_network:
        return
    try:
        loopback = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        loopback = parsed.hostname == "localhost"
    if not loopback:
        raise ValueError("non-loopback Home Assistant connections require wss")


def _light_report(entity_id: str, raw: object) -> LightReport:
    if not isinstance(raw, Mapping) or raw.get("entity_id") != entity_id:
        raise HomeAssistantLightError("allowed light state is missing or malformed")
    state = raw.get("state")
    if not isinstance(state, str):
        raise HomeAssistantLightError("light state is malformed")
    if state in {"unavailable", "unknown"}:
        return LightReport(entity_id, None, False)
    attributes = raw.get("attributes")
    if not isinstance(attributes, Mapping):
        raise HomeAssistantLightError("light attributes are missing")
    modes = attributes.get("supported_color_modes")
    if not isinstance(modes, list) or not modes or not all(isinstance(m, str) for m in modes):
        raise HomeAssistantLightError("light brightness support is not declared")
    if not _BRIGHTNESS_MODES.intersection(modes):
        raise HomeAssistantLightError("light does not support brightness")
    if state == "off":
        return LightReport(entity_id, 0.0, True)
    if state != "on":
        raise HomeAssistantLightError("unsupported light state")
    brightness = attributes.get("brightness")
    if type(brightness) is not int or not 1 <= brightness <= 255:
        raise HomeAssistantLightError("on-state brightness is missing or invalid")
    return LightReport(entity_id, brightness / 255, True)


class HomeAssistantLight:
    """A serial, bounded HA connection for one operator-approved light entity.

    There is never more than one pending command. Reconnect is explicit; a lost
    service result is not replayed. The token is retained only in memory and
    excluded from representations and errors.
    """

    def __init__(
        self,
        websocket_url: str,
        access_token: str,
        entity_id: str,
        *,
        allow_insecure_network: bool = False,
        command_timeout_s: float = 5.0,
        state_timeout_s: float = 3.0,
    ) -> None:
        _validate_url(websocket_url, allow_insecure_network=allow_insecure_network)
        if not access_token or not isinstance(access_token, str):
            raise ValueError("Home Assistant access token must be nonempty")
        if not entity_id.startswith("light.") or len(entity_id) <= len("light."):
            raise ValueError("an explicit light entity ID is required")
        if not all(
            math.isfinite(value) and value > 0 for value in (command_timeout_s, state_timeout_s)
        ):
            raise ValueError("Home Assistant timeouts must be finite and positive")
        self.entity_id = entity_id
        self._url = websocket_url
        self._token = access_token
        self._command_timeout_s = command_timeout_s
        self._state_timeout_s = state_timeout_s
        self._lock = asyncio.Lock()
        self._session: aiohttp.ClientSession | None = None
        self._socket: aiohttp.ClientWebSocketResponse | None = None
        self._subscription_id: int | None = None
        self._next_id = 1
        self._state: object = None
        self._generation = 0
        self._uncertain = False
        self._closed = False

    @property
    def generation(self) -> int:
        return self._generation

    async def _drop(self) -> None:
        socket, session = self._socket, self._session
        self._socket = None
        self._session = None
        self._subscription_id = None
        self._state = None
        if socket is not None:
            await socket.close()
        if session is not None:
            await session.close()

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            await self._drop()

    async def connect(self) -> None:
        """Open or explicitly refresh the connection and filtered state view."""
        async with self._lock:
            if self._closed:
                raise HomeAssistantLightError("light adapter is closed")
            if self._socket is not None and not self._socket.closed:
                return
            await self._open()

    async def reconnect(self) -> None:
        """Refresh state after an uncertain command without replaying that command."""
        async with self._lock:
            if self._closed:
                raise HomeAssistantLightError("light adapter is closed")
            await self._drop()
            await self._open()
            self._uncertain = False

    async def _open(self) -> None:
        try:
            self._session = aiohttp.ClientSession()
            self._socket = await asyncio.wait_for(
                self._session.ws_connect(self._url, heartbeat=15, max_msg_size=4_194_304),
                self._command_timeout_s,
            )
            required = await self._receive(self._command_timeout_s)
            if required.get("type") != "auth_required":
                raise HomeAssistantLightError("Home Assistant auth handshake is invalid")
            await asyncio.wait_for(
                self._socket.send_json({"type": "auth", "access_token": self._token}),
                self._command_timeout_s,
            )
            authenticated = await self._receive(self._command_timeout_s)
            if authenticated.get("type") != "auth_ok":
                raise HomeAssistantLightError("Home Assistant authentication failed")
            self._next_id = 1
            subscription_id, _ = await self._command(
                {"type": "subscribe_events", "event_type": "state_changed"}
            )
            self._subscription_id = subscription_id
            await self._refresh_state()
            self._generation += 1
        except Exception as error:
            await self._drop()
            if isinstance(error, HomeAssistantLightError):
                raise
            raise HomeAssistantLightError("Home Assistant connection failed") from error

    async def _receive(self, timeout_s: float) -> Mapping[str, Any]:
        if self._socket is None:
            raise HomeAssistantLightError("Home Assistant is disconnected")
        message = await asyncio.wait_for(self._socket.receive_json(), timeout_s)
        if not isinstance(message, Mapping) or not isinstance(message.get("type"), str):
            raise HomeAssistantLightError("Home Assistant message is malformed")
        return message

    def _accept_event(self, message: Mapping[str, Any]) -> None:
        if message.get("id") != self._subscription_id:
            raise HomeAssistantLightError("unexpected Home Assistant event subscription")
        event = message.get("event")
        if not isinstance(event, Mapping) or event.get("event_type") != "state_changed":
            raise HomeAssistantLightError("unexpected Home Assistant event")
        data = event.get("data")
        if isinstance(data, Mapping) and data.get("entity_id") == self.entity_id:
            self._state = data.get("new_state")

    async def _command(self, command: Mapping[str, object]) -> tuple[int, object]:
        if self._socket is None:
            raise HomeAssistantLightError("Home Assistant is disconnected")
        command_id = self._next_id
        self._next_id += 1
        await asyncio.wait_for(
            self._socket.send_json({"id": command_id, **command}),
            self._command_timeout_s,
        )
        deadline = time.monotonic() + self._command_timeout_s
        for _ in range(64):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            message = await self._receive(remaining)
            if message["type"] == "event":
                self._accept_event(message)
                continue
            if message["type"] != "result" or message.get("id") != command_id:
                raise HomeAssistantLightError("Home Assistant response ID or type is invalid")
            if message.get("success") is not True:
                raise HomeAssistantLightError("Home Assistant command was rejected")
            return command_id, message.get("result")
        raise HomeAssistantLightError("Home Assistant response deadline exceeded")

    async def _refresh_state(self) -> LightReport:
        _, result = await self._command({"type": "get_states"})
        if not isinstance(result, list):
            raise HomeAssistantLightError("Home Assistant state dump is malformed")
        matches = [
            state
            for state in result
            if isinstance(state, Mapping) and state.get("entity_id") == self.entity_id
        ]
        if len(matches) != 1:
            raise HomeAssistantLightError("allowed light is missing from state dump")
        self._state = matches[0]
        return _light_report(self.entity_id, self._state)

    async def _wait_for_level(self, level: float) -> LightReport:
        report = await self._refresh_state()
        while True:
            if (
                report.available
                and report.level is not None
                and (abs(report.level - level) <= 1 / 255 + 1e-9)
            ):
                return report
            message = await self._receive(self._command_timeout_s)
            if message["type"] != "event":
                raise HomeAssistantLightError("unexpected Home Assistant readback message")
            self._accept_event(message)
            report = _light_report(self.entity_id, self._state)

    async def current_state(self) -> LightReport:
        async with self._lock:
            if self._closed:
                raise HomeAssistantLightError("light adapter is closed")
            if self._socket is None or self._socket.closed:
                await self._open()
            return await self._refresh_state()

    async def set_brightness(self, level: float) -> LightReport:
        if (
            isinstance(level, bool)
            or not isinstance(level, (int, float))
            or not (math.isfinite(level) and 0 <= level <= 0.75)
        ):
            raise ValueError("brightness must be an absolute level in [0, 0.75]")
        async with self._lock:
            if self._closed:
                raise HomeAssistantLightError("light adapter is closed")
            if self._uncertain:
                raise HomeAssistantLightError("light effect requires explicit reconciliation")
            if self._socket is None or self._socket.closed:
                await self._open()
            prior = await self._refresh_state()
            if not prior.available:
                raise HomeAssistantLightError("allowed light is unavailable")
            command: dict[str, object] = {
                "type": "call_service",
                "domain": "light",
                "service": "turn_off" if level == 0 else "turn_on",
                "target": {"entity_id": self.entity_id},
            }
            if level > 0:
                command["service_data"] = {"brightness": max(1, round(level * 255))}
            try:
                await self._command(command)
                try:
                    return await asyncio.wait_for(
                        self._wait_for_level(level), self._state_timeout_s
                    )
                except TimeoutError as error:
                    raise HomeAssistantLightError("light state was not confirmed") from error
            except (Exception, asyncio.CancelledError) as error:
                # A service call can have taken effect even when its result is lost.
                # Cancellation/deadline expiry does not undo the native write.
                # Drop this generation and never replay the command automatically.
                self._uncertain = True
                await self._drop()
                if isinstance(error, (HomeAssistantLightError, asyncio.CancelledError)):
                    raise
                raise HomeAssistantLightError("light command outcome is uncertain") from error
