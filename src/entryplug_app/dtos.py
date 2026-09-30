"""Public response fields shared by OpenAPI and generated console types.

Records may gain additive fields within v1; lifecycle enums come from the domain.
Adapter-specific evidence remains JSON data, never a provider's private state.
"""

from __future__ import annotations

from enum import Enum
from types import UnionType
from typing import Any, Literal, TypedDict, get_args, get_origin, get_type_hints, is_typeddict

from entryplug.core.operation import EffectState, Lifecycle
from entryplug_app.contracts import Activity, Health, MissionLifecycle


class MissionRecord(TypedDict):
    id: str
    mission_id: str
    revision: int
    definition: dict[str, Any]
    instructions_hash: str


class MissionRun(TypedDict):
    id: str
    run_id: str
    mission_id: str
    name: str
    revision: int
    body_id: str
    session_runtime_id: str
    lifecycle: MissionLifecycle
    activity: Activity
    health: Health
    reason_code: str | None
    active_turn_id: str | None
    last_observation: dict[str, Any] | None
    last_result: str | None
    pending: list[dict[str, Any]]
    model_calls: int
    tool_calls: int
    turn_count: int
    dropped_events: int


class OperationRecord(TypedDict):
    id: str
    attachment_id: str | None
    run_id: str | None
    turn_id: str | None
    body_id: str
    capability: str
    session_runtime_id: str
    native_operation_id: str | None
    request_id: str
    lifecycle: Lifecycle
    effect_state: EffectState
    physical_effects: bool
    evidence_ids: list[str]


class ServiceEvent(TypedDict):
    schema_version: int
    workspace_id: str
    service_instance_id: str
    seq: int
    at: str
    type: str
    run_id: str | None
    turn_id: str | None
    operation_id: str | None
    data: dict[str, Any]


class Topology(TypedDict):
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]


class Snapshot(TypedDict):
    api_version: str
    build_id: str
    workspace_id: str
    service_instance_id: str
    cursor: int
    missions: list[MissionRecord]
    runs: list[MissionRun]
    operations: list[OperationRecord]
    turns: list[dict[str, Any]]
    alerts: list[dict[str, Any]]
    bodies: list[dict[str, Any]]
    connections: list[dict[str, Any]]
    attachments: list[dict[str, Any]]
    approvals: list[dict[str, Any]]
    topology: Topology
    simulations: list[dict[str, Any]]


def response_schemas() -> dict[str, Any]:
    def schema(kind: Any) -> dict[str, Any]:
        origin, args = get_origin(kind), get_args(kind)
        if kind is Any:
            return {}
        if origin is list:
            return {"type": "array", "items": schema(args[0])}
        if origin is dict:
            return {"type": "object", "additionalProperties": schema(args[1])}
        if origin is UnionType:
            return {"anyOf": [schema(arg) for arg in args]}
        if origin is Literal:
            return {"type": "string", "enum": list(args)}
        if is_typeddict(kind):
            return {
                "type": "object",
                "properties": {k: schema(t) for k, t in get_type_hints(kind).items()},
                "required": sorted(kind.__required_keys__),
            }
        if isinstance(kind, type) and issubclass(kind, Enum):
            return {"$ref": "#/components/schemas/" + kind.__name__}
        return {"type": {str: "string", int: "integer", bool: "boolean", type(None): "null"}[kind]}

    result = {
        kind.__name__: schema(kind)
        for kind in (MissionRecord, MissionRun, OperationRecord, ServiceEvent, Snapshot)
    }
    result.update(
        {
            kind.__name__: {"type": "string", "enum": [v.value for v in kind]}
            for kind in (Lifecycle, EffectState)
        }
    )
    return result
