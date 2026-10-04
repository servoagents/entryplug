"""Evaluator-only WebSocket relay; suppress one real service result after application."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import aiohttp
from aiohttp import web


@asynccontextmanager
async def lose_service_reply(
    upstream_url: str,
    observe_applied: Callable[[], Awaitable[dict[str, object]]],
    *,
    after_applied: Callable[[], Awaitable[None]] | None = None,
) -> AsyncIterator[tuple[str, dict[str, object]]]:
    # Retain only bounded native identifiers and evaluator observations, never
    # auth payloads, arbitrary messages or the access token.
    evidence: dict[str, object] = {"service_calls": 0, "dropped_results": 0}

    async def handle(request: web.Request) -> web.WebSocketResponse:
        downstream = web.WebSocketResponse()
        await downstream.prepare(request)
        async with aiohttp.ClientSession() as client:
            async with client.ws_connect(upstream_url) as upstream:
                pending: set[int] = set()

                async def forward_commands() -> None:
                    async for message in downstream:
                        if message.type != aiohttp.WSMsgType.TEXT:
                            break
                        value = json.loads(message.data)
                        if value.get("type") == "call_service":
                            pending.add(value["id"])
                            evidence["service_calls"] += 1
                            evidence["command_id"] = value["id"]
                            evidence["entity_id"] = value.get("target", {}).get("entity_id")
                            evidence["brightness"] = value.get("service_data", {}).get("brightness")
                        await upstream.send_str(message.data)

                async def forward_results() -> None:
                    async for message in upstream:
                        if message.type != aiohttp.WSMsgType.TEXT:
                            break
                        value = json.loads(message.data)
                        if value.get("type") == "result" and value.get("id") in pending:
                            if value.get("success") is not True:
                                raise RuntimeError("upstream service did not accept the command")
                            evidence["applied"] = await observe_applied()
                            evidence["upstream_success"] = True
                            evidence["dropped_results"] += 1
                            if after_applied is not None:
                                await after_applied()
                            return  # Close without forwarding the actual HA result.
                        await downstream.send_str(message.data)

                tasks = [
                    asyncio.create_task(forward_commands()),
                    asyncio.create_task(forward_results()),
                ]
                try:
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    await downstream.close()
        return downstream

    app = web.Application()
    app.router.add_get("/api/websocket", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        yield f"ws://127.0.0.1:{port}/api/websocket", evidence
    finally:
        await runner.cleanup()
