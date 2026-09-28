"""A2A skill and extension preserve the non-motion effect classification."""

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
from entryplug_a2a.server import _capability_extension, _skill


def _validate(arguments: Mapping[str, JsonValue]) -> Mapping[str, object]:
    return dict(arguments)


async def _run(_: OperationContext, __: Mapping[str, JsonValue]) -> OperationResult:
    return OperationResult(Lifecycle.SUCCEEDED, MotionState.IDLE)


def test_light_skill_and_extension_disclose_physical_effect() -> None:
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
    view = OperationHost((spec,)).observe()
    skill = _skill(view.capabilities[0])
    assert skill is not None
    assert "physical-effect" in skill.tags
    assert "read-only" not in skill.tags
    extension = _capability_extension(view)
    assert extension.params is not None
    capability = extension.params["capabilities"][0]
    assert capability["motionProducing"] is False
    assert capability["physicalEffects"] is True
