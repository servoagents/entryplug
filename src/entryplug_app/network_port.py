"""Native MQTT/Zenoh resources become ordinary Session capabilities."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

from entryplug.core.evidence import JsonValue, json_object
from entryplug.core.operation import (
    CapabilitySpec,
    EffectState,
    Lifecycle,
    MotionState,
    OperationContext,
    OperationHost,
    OperationResult,
)
from entryplug.harness.session import Session
from entryplug_app.contracts import AppError, canonical, digest
from entryplug_app.network_profile import validate_network
from entryplug_app.network_transport import MAX_PAYLOAD, MqttTransport, ZenohTransport
from entryplug_app.ports import EmbodimentPort
from entryplug_bridges.zenoh import Observations


async def network_port(
    config: dict[str, Any], credentials: str | None, *, transport_factory: Any = None
) -> EmbodimentPort:
    profile = validate_network(config)
    profile_hash = digest(profile)
    caches = {
        item["key"]: Observations([item["key"]], item.get("max_age_s", 5))
        for item in profile.get("observations", [])
    }

    def receive(key: str, payload: bytes, **metadata: Any) -> None:
        if key in caches:
            # Leave room for envelope metadata inside the host's 16 KiB result bound.
            caches[key].receive(
                key, payload if len(payload) <= MAX_PAYLOAD else b"x" * 16385, **metadata
            )

    factory = transport_factory or (MqttTransport if config["kind"] == "mqtt" else ZenohTransport)
    transport = factory(config, credentials, receive)
    try:
        await transport.open()
    except BaseException:
        for cache in caches.values():
            cache.close()
        await transport.close()
        raise

    async def health() -> None:
        transport.check()

    def observation(item: dict[str, Any]) -> CapabilitySpec:
        def validate(arguments: Mapping[str, JsonValue]) -> Mapping[str, object]:
            if arguments:
                raise ValueError("This observation takes no arguments")
            return {}

        async def read(
            context: OperationContext, arguments: Mapping[str, JsonValue]
        ) -> OperationResult:
            transport.check()
            sample = caches[item["key"]].read(item["key"])
            return OperationResult(
                Lifecycle.SUCCEEDED if sample["status"] == "ok" else Lifecycle.FAILED,
                MotionState.IDLE,
                {
                    **sample,
                    "provenance": config["kind"] + "_publication",
                    "profile_hash": profile_hash,
                },
                reason_code=None if sample["status"] == "ok" else sample["status"].upper(),
            )

        return CapabilitySpec(
            item["name"],
            "1",
            item["description"] + " Read-only; receive age is not capture age.",
            False,
            5,
            0.2,
            validate,
            read,
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        )

    def action(item: dict[str, Any]) -> CapabilitySpec:
        def validate(arguments: Mapping[str, JsonValue]) -> Mapping[str, object]:
            from entryplug_agents.native import validate_arguments

            value = json_object(arguments, "network arguments")
            try:
                validate_arguments(value, item["input_schema"])
            except AppError as error:
                raise ValueError(str(error)) from error
            return value

        async def run(
            context: OperationContext, arguments: Mapping[str, JsonValue]
        ) -> OperationResult:
            base = {
                "profile_hash": profile_hash,
                "key": item["key"],
                "protocol": config["kind"],
                "provenance": "network_operation",
            }
            if context.cancel_requested or context.remaining_seconds <= 0:
                return OperationResult(Lifecycle.CANCELED, MotionState.IDLE, base, "NOT_DISPATCHED")
            try:
                transport.check()
            except ConnectionError:
                return OperationResult(Lifecycle.FAILED, MotionState.IDLE, base, "SOURCE_LOST")
            payload = canonical(
                {
                    "request_id": context.operation_id,
                    "arguments": json_object(arguments, "arguments"),
                }
            ).encode()
            context.report_effect(EffectState.REQUESTED)
            pending = asyncio.create_task(
                transport.request(
                    item["key"], payload, context.operation_id, context.remaining_seconds
                )
            )
            canceled = asyncio.create_task(context.wait_for_cancel())
            try:
                done, _ = await asyncio.wait(
                    [pending, canceled], return_when=asyncio.FIRST_COMPLETED
                )
                if pending not in done:
                    raise TimeoutError(
                        "Cancellation after dispatch cannot establish a device effect"
                    )
                raw = pending.result()
                if len(raw) > MAX_PAYLOAD:
                    raise ValueError("Oversized device reply")
                reply = json.loads(raw)
                canonical(reply)
                if (
                    not isinstance(reply, dict)
                    or set(reply) != {"request_id", "status", "result"}
                    or reply["request_id"] != context.operation_id
                    or reply["status"] not in {"applied", "rejected"}
                    or not isinstance(reply["result"], dict)
                ):
                    raise ValueError("Device reply does not match the admitted request")
                applied = reply["status"] == "applied"
                context.report_effect(EffectState.REPORTED if applied else EffectState.NONE)
                expired = context.remaining_seconds <= 0
                return OperationResult(
                    Lifecycle.SUCCEEDED
                    if applied and not context.cancel_requested and not expired
                    else Lifecycle.FAILED,
                    MotionState.IDLE,
                    {**base, "provenance": "device_report", "device_reply": reply},
                    reason_code=(
                        "DEADLINE_EXCEEDED"
                        if expired
                        else "CANCELED_AFTER_DISPATCH"
                        if context.cancel_requested
                        else None
                        if applied
                        else "DEVICE_REJECTED"
                    ),
                    effect_state=EffectState.REPORTED if applied else EffectState.NONE,
                )
            except (Exception, asyncio.CancelledError):
                # No resend, rollback, or optimistic success on transport ACKs or lost replies.
                return OperationResult(
                    Lifecycle.INDETERMINATE,
                    MotionState.IDLE,
                    base,
                    "DEVICE_EFFECT_UNCONFIRMED",
                    EffectState.UNKNOWN,
                )
            finally:
                pending.cancel()
                canceled.cancel()
                await asyncio.gather(pending, canceled, return_exceptions=True)

        return CapabilitySpec(
            item["name"],
            "1",
            item["description"] + " Acts on the device. Success is a correlated device report, "
            "not independent physical verification. Cancellation cannot undo a dispatched command.",
            False,
            item.get("timeout_s", 5),
            0.2,
            validate,
            run,
            input_schema=item["input_schema"],
            physical_effects=True,
        )

    host = OperationHost(
        [
            *(observation(item) for item in profile.get("observations", [])),
            *(action(item) for item in profile.get("actions", [])),
        ]
    )

    class NetworkSession(Session):
        async def close(self) -> tuple[Any, ...]:
            try:
                return await super().close()
            finally:
                for cache in caches.values():
                    cache.close()
                await transport.close()

    return EmbodimentPort(
        config["id"],
        config["name"],
        NetworkSession(host, owns_runtime=True),
        connection_id=config["id"],
        health_check=health,
        protocol=config["kind"],
        network_scope={
            "profile_hash": profile_hash,
            "observations": [
                {"capability": item["name"], "key": item["key"]}
                for item in profile.get("observations", [])
            ],
            "actions": [
                {"capability": item["name"], "key": item["key"]}
                for item in profile.get("actions", [])
            ],
            "native_authority": "Configured network identity and native ACLs",
            "agent_authority": "Separate capability grant; actions require approval by default",
            "effect_evidence": "Correlated device report; physical verification is separate",
        },
    )
