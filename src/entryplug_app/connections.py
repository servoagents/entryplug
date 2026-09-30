"""Explicit connections to existing adapters; secrets are environment references."""

from __future__ import annotations

import asyncio
import os
import re
from typing import Any, cast

from entryplug_app.contracts import AppError, text, utc_now
from entryplug_app.ports import homeassistant_port
from entryplug_app.service import new_id


class Connections:
    def __init__(self, service: Any, *, health_interval_s: float = 10):
        self.service = service
        self.health_interval_s = health_interval_s

    async def restore(self) -> None:
        for connection in await self.service.store.list("connections"):
            if connection["status"] != "disconnected":
                self.service.spawn(self.connect(connection))

    async def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        if set(payload) - {
            "kind",
            "name",
            "url",
            "entity_id",
            "token_env",
            "tools",
            "source_protocol",
        }:
            raise AppError(
                "validation_error",
                "Connection accepts configuration and an environment reference only",
            )
        if payload.get("kind") not in {"homeassistant", "mcp", "a2a"}:
            raise AppError(
                "adapter_unavailable",
                "Choose the existing Home Assistant adapter or a read-only MCP binding",
            )
        source_protocol = payload.get("source_protocol")
        if source_protocol is not None and (
            source_protocol not in {"ros2", "zenoh", "mqtt"} or payload["kind"] != "mcp"
        ):
            raise AppError("validation_error", "Native middleware labels require an MCP bridge")
        env = payload.get(
            "token_env",
            {
                "homeassistant": "ENTRYPLUG_HA_TOKEN",
                "mcp": "ENTRYPLUG_MCP_TOKEN",
                "a2a": "ENTRYPLUG_A2A_TOKEN",
            }[payload["kind"]],
        )
        if not isinstance(env, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", env):
            raise AppError("validation_error", "token_env must name a service environment variable")
        connection = {
            "id": new_id("connection"),
            "kind": payload["kind"],
            "source_protocol": source_protocol,
            "name": text(payload.get("name"), "connection name", 160),
            "url": text(payload.get("url"), "Home Assistant URL", 2048),
            "entity_id": payload.get("entity_id"),
            "tools": payload.get("tools", []),
            "token_env": env,
            "status": "configured",
            "provenance": "configured",
            "at": utc_now(),
        }
        # Validate even when the optional adapter is not installed.
        from urllib.parse import urlsplit

        parsed = urlsplit(connection["url"])
        if (
            parsed.scheme
            not in ({"ws", "wss"} if connection["kind"] == "homeassistant" else {"http", "https"})
            or (connection["kind"] == "homeassistant" and parsed.path != "/api/websocket")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise AppError(
                "validation_error", "Expected a credential-free ws(s)://host/api/websocket URL"
            )
        if connection["kind"] == "homeassistant":
            connection["entity_id"] = text(connection["entity_id"], "entity ID", 200)
        elif (
            not isinstance(connection["tools"], list)
            or not connection["tools"]
            or len(connection["tools"]) > 32
            or not all(
                isinstance(t, str) and re.fullmatch(r"[a-zA-Z0-9_.-]{1,100}", t)
                for t in connection["tools"]
            )
        ):
            raise AppError("validation_error", "Select 1–32 explicit MCP tool names")
        async with self.service.lock:
            await self.service._record("connections", connection, "connection.configured")
        self.service.spawn(self.connect(connection))
        return connection

    async def connect(self, connection: dict[str, Any]) -> None:
        try:
            token = os.environ.get(connection["token_env"])
            if not token and connection["kind"] == "homeassistant":
                raise AppError(
                    "credentials_missing", "Connection credential environment variable is absent"
                )
            if connection["kind"] == "homeassistant":
                assert token
                port = await homeassistant_port(connection, token)
            elif connection["kind"] == "mcp":
                from entryplug_app.mcp_port import mcp_port

                port = await mcp_port(connection, token)
            else:
                from entryplug_app.a2a_port import a2a_port

                port = await a2a_port(connection, token)
            async with self.service.lock:
                current = await self.service.store.get("connections", connection["id"])
                if current["status"] == "disconnected":
                    await port.close()
                    return
                port.protocol = connection["kind"]
                port.source_protocol = connection.get("source_protocol")
                self.service.ports[port.body_id] = port
                connection.update(
                    status="connected", provenance="observed", last_seen=utc_now(), reason_code=None
                )
                await self.service._record("connections", connection, "connection.observed")
            cursor = 0
            epoch = (await port.session.observe()).runtime_id
            await self.service.ingest(
                {
                    "source": port.body_id,
                    "epoch": epoch,
                    "cursor": cursor,
                    "type": "source.revalidated",
                    "at": utc_now(),
                    "age_ms": 0,
                }
            )
            # Existing device adapter performs bounded, read-only health exchanges.
            while not self.service.closed:
                await asyncio.sleep(self.health_interval_s)
                current = await self.service.store.get("connections", connection["id"])
                if current["status"] == "disconnected":
                    return
                if port.health_check:
                    try:
                        report = await port.health_check()
                        available = getattr(report, "available", True)
                    except Exception:
                        available = False
                    changed = available != port.available
                    port.available = available
                    if available:
                        port.last_seen = utc_now()
                    if changed:
                        cursor += 1
                        await self.service.ingest(
                            {
                                "source": port.body_id,
                                "epoch": epoch,
                                "cursor": cursor,
                                "type": "source.revalidated" if available else "source.lost",
                                "at": utc_now(),
                                "age_ms": 0,
                            }
                        )
                        connection.update(
                            status="connected" if available else "lost", last_seen=port.last_seen
                        )
                        async with self.service.lock:
                            await self.service._record(
                                "connections", connection, "connection.health"
                            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            connection.update(
                status="unverified",
                reason_code=error.code if isinstance(error, AppError) else "connection_failed",
            )
            async with self.service.lock:
                await self.service._record("connections", connection, "connection.failed")

    async def disconnect(self, identifier: str) -> dict[str, Any]:
        async with self.service.lock:
            connection = await self.service.store.get("connections", identifier)
            connection["status"] = "disconnected"
            port = self.service.ports.pop(identifier, None)
            await self.service._record("connections", connection, "connection.disconnected")
        if port:
            await self.service.ingest(
                {
                    "source": port.body_id,
                    "epoch": new_id("disconnect"),
                    "cursor": 0,
                    "type": "source.lost",
                    "at": utc_now(),
                    "age_ms": 0,
                }
            )
            await port.close()
        return cast(dict[str, Any], connection)
