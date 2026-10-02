"""Canonical, standard-library contracts shared by all mission interfaces."""

from __future__ import annotations

import hashlib
import json
import math
import tomllib
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, get_args, get_origin, get_type_hints

API_VERSION = "1"
BUILD_ID = "mission-workbench-v3"
MAX_TEXT_BYTES = 65_536
MAX_REQUEST_BYTES = 131_072
NUMERIC_LIMITS: dict[str, tuple[float, float]] = {
    "max_pending": (1, 64),
    "interval_s": (0.1, 86400),
    "coalesce_window_ms": (0, 60000),
    "max_age_ms": (1, 60000),
    "recent_snapshots": (1, 4),
    "max_concurrent_turns": (1, 1),
    "max_model_calls_per_turn": (1, 10),
    "max_tool_calls_per_turn": (1, 32),
    "turn_timeout_s": (0.1, 300),
    "max_turns_per_hour": (1, 3600),
}


class AppError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, *, retryable: bool = False):
        super().__init__(message)
        self.code, self.status, self.retryable = code, status, retryable


class MissionLifecycle(StrEnum):
    CREATED = "created"
    STARTING = "starting"
    ACTIVE = "active"
    PAUSING = "pausing"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    COMPLETED = "completed"
    FAILED = "failed"


class Activity(StrEnum):
    ACQUIRING = "acquiring"
    WAITING_EVENT = "waiting_event"
    REASONING = "reasoning"
    EXECUTING = "executing"
    WAITING_APPROVAL = "waiting_approval"
    RECOVERING = "recovering"
    IDLE = "idle"


class Health(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


RUN_TERMINAL = frozenset({"stopped", "completed", "failed"})


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def canonical(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise AppError("validation_error", "Expected finite JSON data") from error


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def text(value: Any, name: str = "text", limit: int = MAX_TEXT_BYTES) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.encode()) > limit:
        raise AppError(
            "validation_error", f"{name} must be nonempty UTF-8 text up to {limit} bytes"
        )
    return value


@dataclass(frozen=True)
class AgentConfig:
    driver: Literal["rules", "native", "scripted", "simulated"] = "rules"
    decision_mode: Literal["rules", "assisted"] = "rules"
    profile: str = ""


@dataclass(frozen=True)
class BodyConfig:
    selector: str = "demo.access-camera"
    required_capabilities: list[str] = field(default_factory=lambda: ["person.events"])


@dataclass(frozen=True)
class TriggerConfig:
    kind: Literal["manual", "event", "interval"] = "event"
    source: str = "demo.access-camera"
    event: str = "person.entered"
    interval_s: float = 30.0
    coalesce_window_ms: int = 500
    max_pending: int = 4


@dataclass(frozen=True)
class ObservationConfig:
    max_age_ms: int = 2000
    include_snapshot: bool = True
    recent_snapshots: int = 2


@dataclass(frozen=True)
class AccessConfig:
    allow: list[str] = field(
        default_factory=lambda: ["person.events", "camera.snapshot", "alerts.emit"]
    )
    approve: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Limits:
    max_concurrent_turns: int = 1
    max_model_calls_per_turn: int = 3
    max_tool_calls_per_turn: int = 6
    turn_timeout_s: float = 45.0
    max_turns_per_hour: int = 60


@dataclass(frozen=True)
class AlertConfig:
    sink: Literal["inbox"] = "inbox"
    deduplicate_by: Literal["occurrence"] = "occurrence"


@dataclass(frozen=True)
class RecoveryConfig:
    on_source_loss: Literal["block"] = "block"
    resume_after_revalidation: bool = True
    missed_events: Literal["report_gap"] = "report_gap"


@dataclass(frozen=True)
class MissionDefinition:
    name: str
    instructions: str
    schema_version: Literal[1] = 1
    mode: Literal["once", "watch"] = "watch"
    agent: AgentConfig = field(default_factory=AgentConfig)
    body: BodyConfig = field(default_factory=BodyConfig)
    trigger: TriggerConfig = field(default_factory=TriggerConfig)
    observation: ObservationConfig = field(default_factory=ObservationConfig)
    access: AccessConfig = field(default_factory=AccessConfig)
    limits: Limits = field(default_factory=Limits)
    alerts: AlertConfig = field(default_factory=AlertConfig)
    recovery: RecoveryConfig = field(default_factory=RecoveryConfig)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _decode(kind: Any, value: Any, path: str) -> Any:
    origin, args = get_origin(kind), get_args(kind)
    if origin is Literal:
        if not any(type(value) is type(item) and value == item for item in args):
            raise AppError("validation_error", f"{path} must be one of {args}")
    elif origin is list:
        if not isinstance(value, list) or len(value) > 128:
            raise AppError("validation_error", f"{path} must be a list of at most 128 items")
        return [_decode(args[0], item, path) for item in value]
    elif hasattr(kind, "__dataclass_fields__"):
        if not isinstance(value, dict):
            raise AppError("validation_error", f"{path} must be an object")
        hints = get_type_hints(kind)
        if unknown := set(value) - hints.keys():
            raise AppError("validation_error", f"Unknown {path} fields: {sorted(unknown)}")
        try:
            return kind(
                **{key: _decode(hints[key], item, f"{path}.{key}") for key, item in value.items()}
            )
        except TypeError as error:
            raise AppError("validation_error", f"Missing required fields in {path}") from error
    elif kind is float:
        if type(value) not in (int, float) or not math.isfinite(value):
            raise AppError("validation_error", f"{path} must be a finite number")
        return float(value)
    elif type(value) is not kind:
        raise AppError("validation_error", f"Invalid type for {path}")
    return value


def validate_definition(value: Any) -> MissionDefinition:
    if len(canonical(value).encode()) > MAX_REQUEST_BYTES:
        raise AppError("validation_error", "Mission definition is too large")
    definition: MissionDefinition = _decode(MissionDefinition, value, "mission")
    text(definition.name, "name", 160)
    text(definition.instructions, "instructions")
    text(definition.body.selector, "body selector", 200)
    for group in (definition.trigger, definition.observation, definition.limits):
        for field_name, number in asdict(group).items():
            if field_name in NUMERIC_LIMITS:
                low, high = NUMERIC_LIMITS[field_name]
                if not low <= number <= high:
                    raise AppError("validation_error", f"{field_name} must be within {low}..{high}")
    if definition.agent.driver == "rules":
        if definition.agent.profile or definition.agent.decision_mode != "rules":
            raise AppError(
                "validation_error", "Rules do not accept a model profile or assisted mode"
            )
        if definition.trigger.kind == "event" and definition.trigger.event != "person.entered":
            raise AppError(
                "validation_error", "The entry rule requires a validated person.entered event"
            )
    elif definition.agent.decision_mode != "assisted":
        raise AppError("validation_error", "Agent drivers require assisted mode")
    if definition.agent.driver == "native" and not definition.agent.profile:
        raise AppError("validation_error", "Native inference requires an explicit profile")
    if "alerts.emit" in definition.access.approve:
        raise AppError("validation_error", "The local inbox does not require per-alert approval")
    if not set(definition.access.approve) <= set(definition.access.allow):
        raise AppError("validation_error", "Approval capabilities must also be allowed")
    if not set(definition.body.required_capabilities) <= set(definition.access.allow):
        raise AppError("validation_error", "Required capabilities must be explicitly allowed")
    if definition.trigger.kind == "event" and definition.trigger.source != definition.body.selector:
        raise AppError("validation_error", "The trigger must belong to the selected body")
    return definition


def load_definition(path: Path) -> MissionDefinition:
    raw = path.read_bytes()
    if len(raw) > MAX_REQUEST_BYTES:
        raise AppError("validation_error", "Definition file is too large")
    try:
        value = json.loads(raw) if path.suffix == ".json" else tomllib.loads(raw.decode("utf-8"))
        if "instructions_file" in value:
            if "instructions" in value:
                raise AppError(
                    "validation_error", "Specify instructions or instructions_file, not both"
                )
            instruction_path = path.parent / text(
                value.pop("instructions_file"), "instructions_file", 4096
            )
            with instruction_path.open("rb") as stream:
                content = stream.read(MAX_TEXT_BYTES + 1)
            value["instructions"] = text(content.decode("utf-8"), "instructions")
    except (UnicodeError, ValueError) as error:
        raise AppError(
            "validation_error", "Definition and instructions must be valid UTF-8 TOML/JSON"
        ) from error
    return validate_definition(value)


def definition_schema() -> dict[str, Any]:
    """Generate JSON Schema from the same dataclasses used by every validator."""
    from dataclasses import MISSING

    def schema(kind: Any) -> dict[str, Any]:
        origin, args = get_origin(kind), get_args(kind)
        if origin is Literal:
            return {"enum": list(args), "type": "string" if isinstance(args[0], str) else "integer"}
        if origin is list:
            return {"type": "array", "items": schema(args[0]), "maxItems": 128}
        if hasattr(kind, "__dataclass_fields__"):
            hints = get_type_hints(kind)
            return {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    f.name: schema(hints[f.name])
                    | (
                        {"minimum": NUMERIC_LIMITS[f.name][0], "maximum": NUMERIC_LIMITS[f.name][1]}
                        if f.name in NUMERIC_LIMITS
                        else {}
                    )
                    for f in fields(kind)
                },
                "required": [
                    f.name
                    for f in fields(kind)
                    if f.default is MISSING and f.default_factory is MISSING
                ],
            }
        return {"type": {str: "string", int: "integer", float: "number", bool: "boolean"}[kind]}

    return schema(MissionDefinition)
