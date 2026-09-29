"""Registry identity checks below the live, version-pinned HA fixture."""

from __future__ import annotations

import asyncio

import pytest
from aiohttp import web

from entryplug_homeassistant.light import HomeAssistantLightError
from entryplug_homeassistant.registry import resolve_mqtt_light


def test_mqtt_light_is_resolved_by_unique_id_not_display_name() -> None:
    async def websocket(request: web.Request) -> web.WebSocketResponse:
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        await socket.send_json({"type": "auth_required"})
        assert (await socket.receive_json()) == {"type": "auth", "access_token": "test-token"}
        await socket.send_json({"type": "auth_ok"})
        assert (await socket.receive_json()) == {"id": 1, "type": "config/entity_registry/list"}
        await socket.send_json(
            {
                "id": 1,
                "type": "result",
                "success": True,
                "result": [
                    {
                        "platform": "mqtt",
                        "unique_id": "fixture-one",
                        "entity_id": "light.actual_registry_id",
                        "name": "Not the ID",
                        "disabled_by": None,
                    },
                    {
                        "platform": "mqtt",
                        "unique_id": "fixture-two",
                        "entity_id": "light.other",
                        "disabled_by": None,
                    },
                ],
            }
        )
        return socket

    async def scenario() -> None:
        app = web.Application()
        app.router.add_get("/api/websocket", websocket)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        assert site._server is not None
        port = site._server.sockets[0].getsockname()[1]
        url = f"ws://127.0.0.1:{port}/api/websocket"
        try:
            assert await resolve_mqtt_light(url, "test-token", "fixture-one") == (
                "light.actual_registry_id"
            )
            with pytest.raises(HomeAssistantLightError, match="missing or ambiguous"):
                await resolve_mqtt_light(url, "test-token", "not-registered")
        finally:
            await runner.cleanup()

    asyncio.run(scenario())
