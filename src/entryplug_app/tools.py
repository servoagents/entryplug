"""The only tool authority available to a managed turn."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from entryplug_app.contracts import AppError

if TYPE_CHECKING:
    from entryplug_app.drivers import TurnContext
    from entryplug_app.service import ApplicationService


class ToolBroker:
    def __init__(self, service: ApplicationService, context: TurnContext):
        self.service, self.context = service, context

    async def catalog(self) -> list[dict[str, Any]]:
        definition = self.context.definition
        port = self.service.ports[definition.body.selector]
        tools = [
            {"name": c["name"], "description": c["description"], "parameters": c["input_schema"]}
            for c in (await port.describe())["capabilities"]
            if c["name"] in definition.access.allow
        ]
        if "alerts.emit" in definition.access.allow:
            tools.append(
                {
                    "name": "alerts.emit",
                    "description": "Create a persistent inbox alert backed by this turn's evidence",
                    "parameters": {
                        "type": "object",
                        "properties": {"message": {"type": "string"}},
                        "required": ["message"],
                        "additionalProperties": False,
                    },
                }
            )
        return tools

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        context = self.context
        if name not in context.definition.access.allow:
            raise AppError("forbidden", "Tool is outside this turn's grant", 403)
        if context.tool_calls >= context.definition.limits.max_tool_calls_per_turn:
            raise AppError("tool_limit", "Turn tool-call limit reached", 409)
        if not isinstance(arguments, dict):
            raise AppError("validation_error", "Tool arguments must be complete objects")
        context.tool_calls += 1
        if name == "alerts.emit":
            if set(arguments) != {"message"}:
                raise AppError("validation_error", "alerts.emit requires only message")
            return await self.service.emit_alert(context, arguments["message"])
        op = await self.service.command(
            "operation.create",
            {
                "run_id": context.run_id,
                "turn_id": context.turn_id,
                "capability": name,
                "arguments": arguments,
            },
            f"{context.turn_id}:{context.tool_calls}",
        )
        async with self.service.lock:
            run = await self.service.store.get("runs", context.run_id)
            run["activity"] = (
                "waiting_approval" if op["dispatch"] == "waiting_approval" else "executing"
            )
            await self.service._record("runs", run, "turn.tool", context.run_id)
        while op["lifecycle"] in {"accepted", "running", "canceling"}:
            await asyncio.sleep(0.02)
            op = await self.service.store.get("operations", op["id"])
        context.evidence_ids.extend(op["evidence_ids"])
        if op.get("result", {}).get("age_ms", 0) > context.definition.observation.max_age_ms:
            raise AppError(
                "stale_observation", "Tool observation is older than the mission permits", 409
            )
        if context.definition.observation.include_snapshot:
            from entryplug_app.media import selected_images

            images = []
            for identifier in op["evidence_ids"]:
                evidence = await self.service.store.get("evidence", identifier)
                images.extend(
                    selected_images(
                        evidence["content"], context.definition.observation.recent_snapshots
                    )
                )
            if images:
                return {
                    **op,
                    "_selected_images": images[: context.definition.observation.recent_snapshots],
                }
        return op
