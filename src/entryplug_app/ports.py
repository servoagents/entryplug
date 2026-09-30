"""Thin embodiment ports. Admission and physical effects remain in Session."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from entryplug.core.evidence import json_object
from entryplug.harness.session import Session
from entryplug_app.contracts import utc_now


@dataclass
class EmbodimentPort:
    body_id: str
    name: str
    session: Session
    simulated: bool = False
    provenance: str = "observed"
    available: bool = True
    first_seen: str = field(default_factory=utc_now)
    last_seen: str = field(default_factory=utc_now)
    connection_id: str | None = None
    health_check: Callable[[], Awaitable[Any]] | None = None

    body_type: str = "device"
    protocol: str = "session"
    source_protocol: str | None = None

    async def describe(self) -> dict[str, Any]:
        view = await self.session.observe()
        return {
            "id": self.body_id,
            "name": self.name,
            "body_type": self.body_type,
            "protocol": self.protocol,
            "source_protocol": self.source_protocol,
            "simulated": self.simulated,
            "provenance": self.provenance,
            "status": "available" if self.available else "lost",
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "runtime_id": view.runtime_id,
            "connection_id": self.connection_id,
            "capabilities": [json_object(spec, "capability") for spec in view.capabilities],
            "discovery": "explicit binding",
            "replay": False,
        }

    async def inspect(self, native_operation_id: str) -> dict[str, Any]:
        return json_object(await self.session.inspect(native_operation_id, "result"), "operation")

    async def close(self) -> None:
        await self.session.close()


async def homeassistant_port(config: dict[str, Any], token: str) -> EmbodimentPort:
    """Expose the existing adapter's read-only device report through the real host."""
    from entryplug.core.operation import (
        CapabilitySpec,
        Lifecycle,
        MotionState,
        OperationContext,
        OperationHost,
        OperationResult,
    )
    from entryplug_homeassistant.light import HomeAssistantLight

    light = HomeAssistantLight(config["url"], token, config["entity_id"])
    await light.connect()

    def validate(arguments: Any) -> dict[str, Any]:
        if arguments:
            raise ValueError("light state takes no arguments")
        return {}

    async def read(context: OperationContext, arguments: Any) -> OperationResult:
        state = await light.current_state()
        return OperationResult(
            Lifecycle.SUCCEEDED,
            MotionState.IDLE,
            {
                "entity_id": state.entity_id,
                "level": state.level,
                "available": state.available,
                "generation": light.generation,
                "provenance": "device_report",
                "at": utc_now(),
            },
        )

    host = OperationHost(
        [
            CapabilitySpec(
                name="homeassistant.light_state",
                version="1",
                description="Read the explicitly connected light's device report",
                motion_producing=False,
                deadline_seconds=10,
                cancel_grace_seconds=1,
                validate=validate,
                run=read,
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            )
        ]
    )

    class LightSession(Session):
        async def close(self) -> tuple[Any, ...]:
            try:
                return await super().close()
            finally:
                await light.close()

    return EmbodimentPort(
        config["id"],
        config["name"],
        LightSession(host, owns_runtime=True),
        connection_id=config["id"],
        health_check=light.current_state,
    )
