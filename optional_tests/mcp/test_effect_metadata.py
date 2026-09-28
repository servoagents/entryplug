"""Non-motion physical writes must not be advertised as read-only tools."""

from __future__ import annotations

from collections.abc import Mapping

from entryplug.core.evidence import JsonValue
from entryplug.core.operation import (
    CapabilitySpec,
    Lifecycle,
    MotionState,
    OperationContext,
    OperationHost,
    OperationResult,
)
from entryplug_mcp.server import _capability_tool


def _validate(arguments: Mapping[str, JsonValue]) -> Mapping[str, object]:
    return dict(arguments)


async def _run(_: OperationContext, __: Mapping[str, JsonValue]) -> OperationResult:
    return OperationResult(Lifecycle.SUCCEEDED, MotionState.IDLE)


def test_light_tool_discloses_physical_effect_without_motion() -> None:
    spec = CapabilitySpec(
        "set_light",
        "1",
        "Set a configured light brightness",
        False,
        1.0,
        0.1,
        _validate,
        _run,
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        physical_effects=True,
    )
    capability = OperationHost((spec,)).observe().capabilities[0]
    tool = _capability_tool(capability)
    assert tool is not None
    assert tool.annotations is not None
    assert tool.annotations.read_only_hint is False
    assert tool.annotations.destructive_hint is True
