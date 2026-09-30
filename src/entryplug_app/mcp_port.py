"""Explicit outbound MCP binding, using the official optional SDK.

The initial import is restricted to tools advertised read-only. Remote schemas
are pinned for this binding; a changed catalog closes admission until reconnect.
Remote content is data, never a change to a mission's local grant.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from entryplug.core.evidence import JsonValue, json_object
from entryplug.core.operation import (
    CapabilitySpec,
    Lifecycle,
    MotionState,
    OperationContext,
    OperationHost,
    OperationResult,
)
from entryplug.harness.session import Session
from entryplug_app.contracts import AppError, canonical, digest
from entryplug_app.ports import EmbodimentPort


async def mcp_port(config: dict[str, Any], token: str | None) -> EmbodimentPort:
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    from entryplug_agents.native import validate_arguments

    ready, closing = asyncio.Event(), asyncio.Event()
    state: dict[str, Any] = {}

    async def owner() -> None:
        try:
            headers = {"Authorization": "Bearer " + token} if token else {}
            async with httpx2.AsyncClient(headers=headers, timeout=15, trust_env=False) as http:
                async with Client(
                    streamable_http_client(config["url"], http_client=http)
                ) as client:
                    state["client"] = client
                    state["catalog"] = (await client.list_tools()).tools
                    ready.set()
                    await closing.wait()
        finally:
            ready.set()

    task = asyncio.create_task(owner())
    try:
        async with asyncio.timeout(20):
            await ready.wait()
        if task.done():
            await task
            raise AppError("connection_failed", "MCP connection closed during discovery", 409)
        client = state["client"]
        selected = {t.name: t for t in state["catalog"] if t.name in config["tools"]}
        if set(selected) != set(config["tools"]):
            raise AppError("catalog_changed", "An explicitly selected MCP tool is missing", 409)
        if any(
            not t.annotations or t.annotations.read_only_hint is not True for t in selected.values()
        ):
            raise AppError("scope_unsupported", "Initial MCP imports require read-only tools", 409)
        catalog_hash = digest({n: t.model_dump(mode="json") for n, t in selected.items()})

        async def health() -> None:
            if task.done():
                raise AppError("source_lost", "MCP transport was lost", 409)
            current = {
                t.name: t.model_dump(mode="json")
                for t in (await client.list_tools()).tools
                if t.name in selected
            }
            if digest(current) != catalog_hash:
                raise AppError(
                    "catalog_changed", "Reconnect to review the changed MCP catalog", 409
                )

        def capability(name: str, remote: Any) -> CapabilitySpec:
            def validate(arguments: Mapping[str, JsonValue]) -> Mapping[str, object]:
                value = dict(arguments)
                validate_arguments(value, remote.input_schema)
                return value

            async def call(
                context: OperationContext, arguments: Mapping[str, JsonValue]
            ) -> OperationResult:
                await health()
                result = await client.call_tool(name, dict(arguments))
                document = result.model_dump(mode="json", by_alias=True, exclude_none=True)
                if len(canonical(document).encode()) > 262144:
                    raise AppError(
                        "result_too_large", "MCP result exceeds the 256 KiB import budget"
                    )
                return OperationResult(
                    Lifecycle.FAILED if result.is_error else Lifecycle.SUCCEEDED,
                    MotionState.IDLE,
                    json_object(
                        {
                            "remote_result": document,
                            "catalog_hash": catalog_hash,
                            "remote_tool": name,
                            "provenance": "remote_report",
                        },
                        "MCP result",
                    ),
                )

            return CapabilitySpec(
                name="mcp." + name,
                version="1",
                description=remote.description or name,
                motion_producing=False,
                physical_effects=False,
                deadline_seconds=20,
                cancel_grace_seconds=1,
                validate=validate,
                run=call,
                input_schema=remote.input_schema,
            )

        host = OperationHost([capability(name, remote) for name, remote in selected.items()])

        class MCPSession(Session):
            async def close(self) -> tuple[Any, ...]:
                try:
                    return await super().close()
                finally:
                    closing.set()
                    await asyncio.gather(task, return_exceptions=True)

        return EmbodimentPort(
            config["id"],
            config["name"],
            MCPSession(host, owns_runtime=True),
            connection_id=config["id"],
            health_check=health,
        )
    except BaseException:
        closing.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise
