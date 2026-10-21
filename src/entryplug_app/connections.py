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
        self.attempts: dict[str, asyncio.Task[Any]] = {}

    async def restore(self) -> None:
        for connection in await self.service.store.list("connections"):
            if connection["status"] != "disconnected":
                self._start(connection)

    async def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        if set(payload) - {
            "kind",
            "name",
            "url",
            "entity_id",
            "token_env",
            "tools",
            "source_protocol",
            "profile",
        }:
            raise AppError(
                "validation_error",
                "Connection accepts configuration and an environment reference only",
            )
        if payload.get("kind") not in {"homeassistant", "mcp", "a2a", "mqtt", "zenoh"}:
            raise AppError(
                "adapter_unavailable",
                "Choose Home Assistant, MCP, A2A, MQTT or Zenoh",
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
                "mqtt": "ENTRYPLUG_MQTT_CREDENTIALS",
                "zenoh": "ENTRYPLUG_ZENOH_CONFIG",
            }[payload["kind"]],
        )
        if not isinstance(env, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", env):
            raise AppError("validation_error", "token_env must name a service environment variable")
        connection = {
            "id": new_id("connection"),
            "kind": payload["kind"],
            "source_protocol": source_protocol,
            "name": text(payload.get("name"), "connection name", 160),
            "url": text(payload.get("url"), "connection endpoint", 2048),
            "entity_id": payload.get("entity_id"),
            "tools": payload.get("tools", []),
            "profile": payload.get("profile"),
            "token_env": env,
            "credentials_required": bool(payload.get("token_env")),
            "status": "configured",
            "provenance": "configured",
            "at": utc_now(),
        }
        if connection["kind"] in {"mqtt", "zenoh"}:
            from entryplug_app.network_profile import normalize_endpoint, validate_network

            if payload.get("tools") or payload.get("entity_id"):
                raise AppError("validation_error", "Native networks use a device profile")
            validate_network(connection)
            connection["url"] = normalize_endpoint(connection["kind"], connection["url"])
        else:
            if payload.get("profile") is not None:
                raise AppError(
                    "validation_error", "Profiles apply only to native network connections"
                )
            # Validate even when the optional adapter is not installed.
            from urllib.parse import urlsplit

            parsed = urlsplit(connection["url"])
            if (
                parsed.scheme
                not in (
                    {"ws", "wss"} if connection["kind"] == "homeassistant" else {"http", "https"}
                )
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
            await self._check_routes(connection)
            await self.service._record("connections", connection, "connection.configured")
        self._start(connection)
        return connection

    async def _check_routes(self, connection: dict[str, Any]) -> None:
        """Retain uncertain resource ownership; allow replacement after a clean disconnect."""
        if connection["kind"] not in {"mqtt", "zenoh"}:
            return
        actions = {item["key"] for item in connection["profile"].get("actions", [])}
        operations = await self.service.store.list("operations")
        for existing in await self.service.store.list("connections"):
            if (
                existing["id"] == connection["id"]
                or existing["kind"] != connection["kind"]
                or existing["url"] != connection["url"]
            ):
                continue
            claimed = {item["key"] for item in (existing.get("profile") or {}).get("actions", [])}
            unresolved = any(
                op["body_id"] == existing["id"]
                and op["physical_effects"]
                and (
                    op["lifecycle"] in {"accepted", "running", "canceling", "indeterminate"}
                    or op.get("effect_state") == "unknown"
                )
                for op in operations
            )
            if actions & claimed and (existing["status"] != "disconnected" or unresolved):
                raise AppError(
                    "resource_already_bound",
                    "An existing connection owns this action route; retry that connection "
                    "to preserve its operation history",
                    409,
                )

    def _start(self, connection: dict[str, Any]) -> bool:
        identifier = connection["id"]
        previous = self.attempts.get(identifier)
        if previous and not previous.done():
            return False
        task = self.service.spawn(self.connect(connection))
        self.attempts[identifier] = task

        def forget(finished: asyncio.Task[Any]) -> None:
            if self.attempts.get(identifier) is finished:
                self.attempts.pop(identifier, None)

        task.add_done_callback(forget)
        return True

    async def retry(self, identifier: str) -> dict[str, Any]:
        port = self.service.ports.get(identifier)
        if port is not None and not port.available:
            await self.disconnect(identifier)
        async with self.service.lock:
            connection = await self.service.store.get("connections", identifier)
            if identifier in self.service.ports:
                raise AppError("already_connected", "Test the connected body instead", 409)
            await self._check_routes(connection)
            if not self._start(connection):
                return cast(dict[str, Any], connection)
            connection.update(status="configured", provenance="configured", reason_code=None)
            await self.service._record("connections", connection, "connection.retrying")
        return cast(dict[str, Any], connection)

    async def probe(self, identifier: str) -> dict[str, Any]:
        connection = await self.service.store.get("connections", identifier)
        port = self.service.ports.get(identifier)
        if not port or not port.health_check or connection["status"] == "disconnected":
            raise AppError("not_connected", "Reconnect this body before testing it", 409)
        try:
            async with asyncio.timeout(20):
                report = await port.health_check()
            available = bool(getattr(report, "available", True))
            reason_code = None if available else "source_unavailable"
        except asyncio.CancelledError:
            raise
        except Exception as error:
            available = False
            reason_code = error.code if isinstance(error, AppError) else "connection_failed"
        current = await self.service.store.get("connections", identifier)
        if current["status"] == "disconnected" or self.service.ports.get(identifier) is not port:
            available, reason_code = False, "disconnected"
        return {
            "id": identifier,
            "available": available,
            "reason_code": reason_code,
            "checked_at": utc_now(),
            "check": "read_only",
        }

    async def connect(self, connection: dict[str, Any]) -> None:
        try:
            token = os.environ.get(connection["token_env"])
            if not token and (
                connection["kind"] == "homeassistant" or connection.get("credentials_required")
            ):
                raise AppError(
                    "credentials_missing", "Connection credential environment variable is absent"
                )
            if connection["kind"] == "homeassistant":
                assert token
                port = await homeassistant_port(connection, token)
            elif connection["kind"] == "mcp":
                from entryplug_app.mcp_port import mcp_port

                port = await mcp_port(connection, token)
            elif connection["kind"] in {"mqtt", "zenoh"}:
                from entryplug_app.network_port import network_port

                port = await network_port(connection, token)
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
            async with self.service.lock:
                current = await self.service.store.get("connections", connection["id"])
                if current["status"] == "disconnected":
                    return
                current.update(
                    status="unverified",
                    reason_code=error.code if isinstance(error, AppError) else "connection_failed",
                )
                await self.service._record("connections", current, "connection.failed")

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
        task = self.attempts.get(identifier)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return cast(dict[str, Any], connection)
