"""Resolve an approved fixture identity through Home Assistant's entity registry."""

from __future__ import annotations

import aiohttp

from entryplug_homeassistant.light import HomeAssistantLightError, _validate_url


async def resolve_mqtt_light(
    websocket_url: str,
    access_token: str,
    unique_id: str,
    *,
    allow_insecure_network: bool = False,
    timeout_s: float = 5.0,
) -> str:
    """Find exactly one enabled MQTT light by registered unique ID, not name."""
    _validate_url(websocket_url, allow_insecure_network=allow_insecure_network)
    if not access_token or not unique_id:
        raise ValueError("token and registered unique ID are required")
    try:
        async with (
            aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_s)) as session,
            session.ws_connect(websocket_url, max_msg_size=4_194_304) as socket,
        ):
            if (await socket.receive_json()).get("type") != "auth_required":
                raise HomeAssistantLightError("Home Assistant registry handshake failed")
            await socket.send_json({"type": "auth", "access_token": access_token})
            if (await socket.receive_json()).get("type") != "auth_ok":
                raise HomeAssistantLightError("Home Assistant registry authentication failed")
            await socket.send_json({"id": 1, "type": "config/entity_registry/list"})
            result = await socket.receive_json()
    except (aiohttp.ClientError, TimeoutError) as error:
        raise HomeAssistantLightError("Home Assistant entity registry is unavailable") from error
    entries = result.get("result")
    if result.get("id") != 1 or result.get("success") is not True or not isinstance(entries, list):
        raise HomeAssistantLightError("Home Assistant entity registry reply is invalid")
    matches = [
        entry.get("entity_id")
        for entry in entries
        if isinstance(entry, dict)
        and entry.get("platform") == "mqtt"
        and entry.get("unique_id") == unique_id
        and entry.get("disabled_by") is None
    ]
    if len(matches) != 1 or not isinstance(matches[0], str) or not matches[0].startswith("light."):
        raise HomeAssistantLightError("registered fixture light is missing or ambiguous")
    return matches[0]
