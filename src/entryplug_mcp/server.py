"""MCP tools generated from an Entryplug session capability catalog."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from typing import Any, cast

from mcp import types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server

from entryplug.evidence import JsonValue
from entryplug.operation import AdmissionError, OperationSnapshot, RuntimeView
from entryplug.session import Session

CATALOG_TOOL = "entryplug_capabilities"
INSPECT_TOOL = "entryplug_inspect"
CANCEL_TOOL = "entryplug_cancel"
_RESERVED_TOOLS = frozenset({CATALOG_TOOL, INSPECT_TOOL, CANCEL_TOOL})
_TRANSPORT_FIELDS = frozenset({"request_id", "runtime_id"})

_OPERATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "operation_id": {"type": "string"},
        "runtime_id": {"type": "string"},
        "request_id": {"type": "string"},
        "capability": {"type": "string"},
        "lifecycle": {"type": "string"},
        "motion_state": {"type": "string"},
        "phase": {"type": "string"},
        "cancel_requested": {"type": "boolean"},
        "reason_code": {"type": ["string", "null"]},
    },
    "required": [
        "operation_id",
        "runtime_id",
        "request_id",
        "capability",
        "lifecycle",
        "motion_state",
        "phase",
        "cancel_requested",
        "reason_code",
    ],
    "additionalProperties": False,
}


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _json_object(value: object) -> dict[str, Any]:
    copied = json.loads(json.dumps(_plain(value), allow_nan=False))
    if not isinstance(copied, dict):
        raise ValueError("MCP structured result must be an object")
    return cast(dict[str, Any], copied)


def _operation_payload(snapshot: OperationSnapshot) -> dict[str, JsonValue]:
    return {
        "operation_id": snapshot.operation_id,
        "runtime_id": snapshot.runtime_id,
        "request_id": snapshot.request_id,
        "capability": snapshot.capability,
        "lifecycle": snapshot.lifecycle.value,
        "motion_state": snapshot.motion_state.value,
        "phase": snapshot.phase,
        "cancel_requested": snapshot.cancel_requested,
        "reason_code": snapshot.reason_code,
    }


def _catalog_payload(view: RuntimeView) -> dict[str, Any]:
    return {
        "runtime_id": view.runtime_id,
        "revision": view.revision,
        "capabilities": _plain(view.capabilities),
        "active_motion_operation": view.active_motion_operation,
        "admission_open": view.admission_open,
        "motion_inhibited_reason": view.motion_inhibited_reason,
    }


def _result(payload: Mapping[str, object]) -> types.CallToolResult:
    structured = _json_object(payload)
    return types.CallToolResult(
        content=[
            types.TextContent(
                type="text",
                text=json.dumps(structured, separators=(",", ":"), sort_keys=True),
            )
        ],
        structured_content=structured,
    )


def _error(reason_code: str, message: str) -> types.CallToolResult:
    payload = {"error": {"reason_code": reason_code, "message": message}}
    return types.CallToolResult(
        content=[
            types.TextContent(
                type="text",
                text=json.dumps(payload, separators=(",", ":"), sort_keys=True),
            )
        ],
        structured_content=payload,
        is_error=True,
    )


def _runtime_arguments(properties: Mapping[str, object]) -> dict[str, object]:
    collisions = _TRANSPORT_FIELDS.intersection(properties)
    if collisions:
        names = ", ".join(sorted(collisions))
        raise ValueError(f"capability schema reserves MCP transport fields: {names}")
    combined = dict(properties)
    combined.update(
        {
            "request_id": {"type": "string", "minLength": 1},
            "runtime_id": {"type": "string", "minLength": 1},
        }
    )
    return combined


def _capability_tool(capability: Mapping[str, JsonValue]) -> types.Tool | None:
    raw_schema = capability.get("input_schema")
    if not isinstance(raw_schema, Mapping):
        return None
    schema = _json_object(raw_schema)
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        raise ValueError("capability input schema properties must be an object")
    schema["properties"] = _runtime_arguments(properties)
    required = schema.get("required", [])
    if not isinstance(required, list):
        raise ValueError("capability input schema required must be an array")
    schema["required"] = sorted(set(required) | _TRANSPORT_FIELDS)
    schema["additionalProperties"] = False

    name = capability.get("name")
    description = capability.get("description")
    motion_producing = capability.get("motion_producing")
    if not isinstance(name, str) or not isinstance(description, str):
        raise ValueError("capability catalog entry has no stable identity")
    if name in _RESERVED_TOOLS:
        raise ValueError(f"capability name is reserved by the MCP adapter: {name}")
    if not isinstance(motion_producing, bool):
        raise ValueError("capability motion classification is missing")
    return types.Tool(
        name=name,
        description=description,
        input_schema=schema,
        output_schema=_OPERATION_SCHEMA,
        annotations=types.ToolAnnotations(
            read_only_hint=not motion_producing,
            destructive_hint=motion_producing,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )


def _control_tools() -> list[types.Tool]:
    runtime_property = {"type": "string", "minLength": 1}
    operation_property = {"type": "string", "minLength": 1}
    return [
        types.Tool(
            name=CATALOG_TOOL,
            description="Read the capability catalog and current admission state",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            annotations=types.ToolAnnotations(
                read_only_hint=True,
                destructive_hint=False,
                idempotent_hint=True,
                open_world_hint=False,
            ),
        ),
        types.Tool(
            name=INSPECT_TOOL,
            description="Inspect one admitted Entryplug operation",
            input_schema={
                "type": "object",
                "properties": {
                    "runtime_id": runtime_property,
                    "operation_id": operation_property,
                    "detail": {"type": "string", "enum": ["summary", "result"]},
                },
                "required": ["runtime_id", "operation_id"],
                "additionalProperties": False,
            },
            annotations=types.ToolAnnotations(
                read_only_hint=True,
                destructive_hint=False,
                idempotent_hint=True,
                open_world_hint=False,
            ),
        ),
        types.Tool(
            name=CANCEL_TOOL,
            description="Request cancellation of one Entryplug operation",
            input_schema={
                "type": "object",
                "properties": {
                    "runtime_id": runtime_property,
                    "operation_id": operation_property,
                },
                "required": ["runtime_id", "operation_id"],
                "additionalProperties": False,
            },
            output_schema=_OPERATION_SCHEMA,
            annotations=types.ToolAnnotations(
                read_only_hint=False,
                destructive_hint=True,
                idempotent_hint=True,
                open_world_hint=False,
            ),
        ),
    ]


def _required_text(arguments: Mapping[str, object], field: str) -> str:
    value = arguments.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a nonempty string")
    return value


def _require_runtime(view: RuntimeView, arguments: Mapping[str, object]) -> None:
    if _required_text(arguments, "runtime_id") != view.runtime_id:
        raise AdmissionError("STALE_RUNTIME", "runtime ID is no longer current")


SessionLifespan = Callable[[Server[Session]], AbstractAsyncContextManager[Session]]


def create_mcp_server(lifespan: SessionLifespan) -> Server[Session]:
    """Create an MCP server that delegates every effect to one session."""

    async def list_tools(
        context: ServerRequestContext[Session],
        _: types.PaginatedRequestParams | None,
    ) -> types.ListToolsResult:
        view = await context.lifespan_context.observe()
        tools = [
            tool
            for capability in view.capabilities
            if (tool := _capability_tool(capability)) is not None
        ]
        tools.extend(_control_tools())
        return types.ListToolsResult(tools=sorted(tools, key=lambda item: item.name))

    async def call_tool(
        context: ServerRequestContext[Session],
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult:
        if params.task is not None:
            return _error("TASKS_UNSUPPORTED", "Entryplug does not advertise MCP Tasks")
        arguments = cast(Mapping[str, object], params.arguments or {})
        session = context.lifespan_context
        try:
            view = await session.observe()
            if params.name == CATALOG_TOOL:
                if arguments:
                    raise ValueError("entryplug_capabilities takes no arguments")
                return _result(_catalog_payload(view))
            if params.name == INSPECT_TOOL:
                _require_runtime(view, arguments)
                detail = arguments.get("detail", "summary")
                if not isinstance(detail, str):
                    raise ValueError("detail must be a string")
                inspected = await session.inspect(_required_text(arguments, "operation_id"), detail)
                return _result(cast(Mapping[str, object], inspected))
            if params.name == CANCEL_TOOL:
                _require_runtime(view, arguments)
                canceled = await session.cancel(_required_text(arguments, "operation_id"))
                return _result(_operation_payload(canceled))

            capability = next(
                (
                    item
                    for item in view.capabilities
                    if item.get("name") == params.name and "input_schema" in item
                ),
                None,
            )
            if capability is None:
                raise AdmissionError("UNKNOWN_CAPABILITY", "capability is not exported")
            runtime_id = _required_text(arguments, "runtime_id")
            request_id = _required_text(arguments, "request_id")
            capability_arguments = {
                key: value for key, value in arguments.items() if key not in _TRANSPORT_FIELDS
            }
            operation = await session.act(
                params.name,
                capability_arguments,
                request_id=request_id,
                expected_runtime_id=runtime_id,
            )
            return _result(_operation_payload(operation))
        except AdmissionError as error:
            return _error(error.reason_code, str(error))
        except KeyError:
            return _error("UNKNOWN_OPERATION", "operation is not known to this runtime")
        except (TypeError, ValueError) as error:
            return _error("INVALID_ARGUMENT", str(error))

    return Server(
        "entryplug",
        version="0.0.1",
        instructions=(
            "Read entryplug_capabilities, submit capabilities with stable request_id and "
            "runtime_id values, then poll entryplug_inspect without using model turns."
        ),
        lifespan=lifespan,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )
