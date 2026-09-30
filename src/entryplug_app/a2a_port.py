"""One-hop, read-only delegation to a versioned Entryplug A2A capability card."""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

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


async def a2a_port(config: dict[str, Any], token: str | None) -> EmbodimentPort:
    import httpx
    from a2a.client import ClientConfig, ClientFactory
    from a2a.extensions.common import find_extension_by_uri
    from a2a.helpers import new_data_part
    from a2a.types import (
        AgentCard,
        CancelTaskRequest,
        GetTaskRequest,
        Message,
        Role,
        SendMessageConfiguration,
        SendMessageRequest,
    )
    from a2a.utils import TransportProtocol

    from entryplug_a2a import CAPABILITY_EXTENSION_URI
    from entryplug_agents.native import validate_arguments

    # Protobuf's generated API has no complete type information in the pinned SDK.
    protobuf = importlib.import_module("google.protobuf.json_format")
    decode: Any = protobuf.MessageToDict
    parse: Any = protobuf.ParseDict
    http = httpx.AsyncClient(
        timeout=15, trust_env=False, headers={"Authorization": "Bearer " + token} if token else {}
    )
    card_url = config["url"].rstrip("/")
    references: dict[str, dict[str, Any]] = {}
    try:
        response = await http.get(card_url)
        response.raise_for_status()
        document = response.json()
        card = parse(document, AgentCard())
        extension = find_extension_by_uri(card, CAPABILITY_EXTENSION_URI)
        if not extension:
            raise AppError("contract_unsupported", "Select an Entryplug capability Agent Card", 409)
        profile = decode(extension.params)
        interfaces = [
            i
            for i in card.supported_interfaces
            if i.protocol_binding == TransportProtocol.HTTP_JSON and i.protocol_version == "1.0"
        ]
        if not interfaces or any(
            urlsplit(i.url).netloc != urlsplit(card_url).netloc for i in interfaces
        ):
            raise AppError(
                "contract_unsupported", "A2A 1.0 endpoints must match the configured authority", 409
            )
        selected = [c for c in profile["capabilities"] if c["name"] in config["tools"]]
        if {c["name"] for c in selected} != set(config["tools"]) or any(
            c["physicalEffects"] or c["name"].startswith(("a2a.", "mcp.")) for c in selected
        ):
            raise AppError(
                "scope_unsupported",
                "Select direct read-only capabilities; delegation depth is one",
                409,
            )
        card_hash = digest(document)
        client = ClientFactory(
            ClientConfig(
                streaming=False,
                polling=False,
                httpx_client=http,
                supported_protocol_bindings=[TransportProtocol.HTTP_JSON],
            )
        ).create(card)

        async def health() -> None:
            current = await http.get(card_url)
            current.raise_for_status()
            if digest(current.json()) != card_hash:
                raise AppError("catalog_changed", "Reconnect to review the changed Agent Card", 409)

        def capability(remote: dict[str, Any]) -> CapabilitySpec:
            def validate(arguments: Mapping[str, JsonValue]) -> Mapping[str, object]:
                validate_arguments(dict(arguments), remote["inputSchema"])
                return dict(arguments)

            async def call(
                context: OperationContext, arguments: Mapping[str, JsonValue]
            ) -> OperationResult:
                await health()
                reference = {
                    "agent_card": card_url,
                    "card_hash": card_hash,
                    "depth": 1,
                    "remote_runtime_id": profile["runtimeId"],
                    "remote_request_id": context.operation_id,
                }
                references[context.operation_id] = reference
                try:
                    events = [
                        event
                        async for event in client.send_message(
                            SendMessageRequest(
                                message=Message(
                                    role=Role.ROLE_USER,
                                    message_id=context.operation_id,
                                    parts=[
                                        new_data_part(
                                            {
                                                "capability": remote["name"],
                                                "runtime_id": profile["runtimeId"],
                                                "request_id": context.operation_id,
                                                "arguments": dict(arguments),
                                            },
                                            media_type="application/json",
                                        )
                                    ],
                                ),
                                configuration=SendMessageConfiguration(return_immediately=True),
                            )
                        )
                    ]
                    task = events[0].task
                    reference.update(task_id=task.id, context_id=task.context_id)
                    cancel_sent = False
                    while True:
                        if task.artifacts:
                            artifact = decode(task.artifacts[0].parts[0].data)
                            if len(canonical(artifact).encode()) > 262144:
                                raise AppError(
                                    "result_too_large", "Delegated result exceeds 256 KiB"
                                )
                            lifecycle = artifact.get("lifecycle", "failed")
                            if lifecycle in {
                                "succeeded",
                                "failed",
                                "canceled",
                                "rejected",
                                "indeterminate",
                            }:
                                return OperationResult(
                                    Lifecycle(lifecycle),
                                    MotionState.IDLE,
                                    json_object(
                                        {"delegation": reference, "remote_result": artifact},
                                        "A2A result",
                                    ),
                                    reason_code=artifact.get("reason_code"),
                                )
                        if context.cancel_requested and not cancel_sent:
                            cancel_sent = True
                            task = await client.cancel_task(CancelTaskRequest(id=task.id))
                        else:
                            await asyncio.sleep(0.05)
                            task = await client.get_task(GetTaskRequest(id=task.id))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    return OperationResult(
                        Lifecycle.INDETERMINATE,
                        MotionState.IDLE,
                        json_object({"delegation": reference}, "A2A uncertainty"),
                        reason_code="DELEGATION_UNCONFIRMED",
                    )

            return CapabilitySpec(
                "a2a." + remote["name"],
                str(remote["version"]),
                "Delegate one read-only task; remote reports retain their own identity",
                False,
                20,
                2,
                validate,
                call,
                remote["inputSchema"],
                physical_effects=False,
            )

        class DelegatedSession(Session):
            async def inspect(
                self, reference: Any, detail: str = "summary"
            ) -> Mapping[str, JsonValue]:
                result = dict(await super().inspect(reference, detail))
                identifier = reference if isinstance(reference, str) else reference.operation_id
                if identifier in references:
                    result["delegation"] = json_object(references[identifier], "delegation")
                return result

            async def close(self) -> tuple[Any, ...]:
                try:
                    return await super().close()
                finally:
                    await client.close()

        return EmbodimentPort(
            config["id"],
            config["name"],
            DelegatedSession(OperationHost([capability(c) for c in selected]), owns_runtime=True),
            connection_id=config["id"],
            health_check=health,
        )
    except BaseException:
        await http.aclose()
        raise
