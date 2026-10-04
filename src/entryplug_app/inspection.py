"""A bounded, no-model mission for an explicitly connected inspection body."""

from __future__ import annotations

from typing import Any

from entryplug_app.contracts import AppError, validate_definition
from entryplug_app.drivers import TurnContext


def inspection_mission(body_id: str, *, approve: bool = True) -> dict[str, Any]:
    """Build an editable mission; a connected body remains the capability authority."""
    return validate_definition(
        {
            "name": "Inspect configured target",
            "instructions": (
                "Obtain a fresh observation of this body's configured target. "
                "The task may adjust its approved light within its declared limits. "
                "Leave the last setting and report the result or uncertainty."
            ),
            "mode": "once",
            "agent": {"driver": "inspection", "decision_mode": "rules"},
            "body": {"selector": body_id, "required_capabilities": ["inspect_target"]},
            "trigger": {"kind": "manual"},
            "access": {
                "allow": ["inspect_target"],
                "approve": ["inspect_target"] if approve else [],
            },
            "limits": {"max_tool_calls_per_turn": 1},
        }
    ).to_dict()


class InspectionDriver:
    """Invoke the configured marker task once through the ordinary tool authority."""

    async def turn(self, context: TurnContext) -> str:
        tool = next(
            (item for item in await context.tools.catalog() if item["name"] == "inspect_target"),
            None,
        )
        target = (
            tool.get("parameters", {}).get("properties", {}).get("target_id", {}).get("const")
            if tool
            else None
        )
        if not isinstance(target, str) or not target:
            raise AppError(
                "inspection_target_unavailable", "Body has no configured inspection target", 409
            )
        operation = await context.tools.call("inspect_target", {"target_id": target})
        if operation["lifecycle"] != "succeeded":
            raise AppError(
                operation.get("reason_code") or "inspection_failed",
                "Inspection did not establish a usable observation; inspect the operation evidence",
                409,
            )
        return (
            "Configured target inspected; fresh observation evidence is attached to the operation."
        )
