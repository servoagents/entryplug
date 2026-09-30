"""Bounded turn contracts and the free, event-driven rule."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from entryplug_app.contracts import MissionDefinition


class ToolPort(Protocol):
    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...
    async def catalog(self) -> list[dict[str, Any]]: ...


@dataclass
class TurnContext:
    run_id: str
    turn_id: str
    definition: MissionDefinition
    observation: dict[str, Any]
    inputs: list[str]
    tools: ToolPort
    model_calls: int = 0
    tool_calls: int = 0
    usage: list[dict[str, Any]] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    on_text: Callable[[str], Awaitable[None]] | None = None


class AgentDriver(Protocol):
    async def turn(self, context: TurnContext) -> str: ...


class RulesDriver:
    async def turn(self, context: TurnContext) -> str:
        if context.observation.get("type") != "person.entered":
            return "No validated entry event; no entry claimed."
        await context.tools.call("alerts.emit", {"message": "Person entered the entrance zone"})
        return "Entry alert recorded with the source's temporal evidence."


class ScriptedDriver:
    """Deterministic assisted-policy fixture, never advertised as real inference."""

    async def turn(self, context: TurnContext) -> str:
        context.model_calls += 1
        context.usage.append({"kind": "scripted", "tokens": 0})
        if context.observation.get("type") == "person.entered":
            await context.tools.call(
                "alerts.emit", {"message": "SIMULATED assisted entry decision"}
            )
        return "SIMULATED scripted turn completed."
