"""One small asynchronous authority for admitted capability operations."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import cast

from entryplug.core.evidence import JsonValue


class Lifecycle(StrEnum):
    ACCEPTED = "accepted"
    RUNNING = "running"
    CANCELING = "canceling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"
    REJECTED = "rejected"
    INDETERMINATE = "indeterminate"


class MotionState(StrEnum):
    IDLE = "idle"
    MOVING = "moving"
    HOLDING = "holding"
    UNKNOWN = "unknown"


class EffectState(StrEnum):
    """Knowledge of a physical effect, separate from arm motion."""

    NONE = "none"
    REQUESTED = "requested"
    REPORTED = "reported"
    OBSERVED = "observed"
    UNKNOWN = "unknown"


TERMINAL_LIFECYCLES = frozenset(
    {
        Lifecycle.SUCCEEDED,
        Lifecycle.FAILED,
        Lifecycle.CANCELED,
        Lifecycle.REJECTED,
        Lifecycle.INDETERMINATE,
    }
)


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _freeze(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _json_object(value: Mapping[str, object], label: str) -> tuple[str, Mapping[str, JsonValue]]:
    try:
        encoded = json.dumps(
            _plain(value),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise AdmissionError("INVALID_ARGUMENT", f"{label} must be finite JSON data") from error
    if len(encoded.encode()) > 16_384:
        raise AdmissionError("INVALID_ARGUMENT", f"{label} exceeds 16384 bytes")
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):
        raise AdmissionError("INVALID_ARGUMENT", f"{label} must be an object")
    return encoded, cast(Mapping[str, JsonValue], _freeze(decoded))


class AdmissionError(RuntimeError):
    """A typed refusal made before a capability handler can cause effects."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class OperationResult:
    """Terminal result returned after the handler reconciles physical state."""

    lifecycle: Lifecycle
    motion_state: MotionState
    result: Mapping[str, object] = field(default_factory=dict)
    reason_code: str | None = None
    effect_state: EffectState = EffectState.NONE

    def __post_init__(self) -> None:
        if self.lifecycle not in TERMINAL_LIFECYCLES - {Lifecycle.REJECTED}:
            raise ValueError("handler result lifecycle must be terminal")
        if not isinstance(self.effect_state, EffectState):
            raise ValueError("effect state must be an EffectState value")


@dataclass(frozen=True, slots=True)
class OperationSnapshot:
    operation_id: str
    runtime_id: str
    request_id: str
    capability: str
    lifecycle: Lifecycle
    motion_state: MotionState
    phase: str
    accepted_monotonic: float
    deadline_monotonic: float
    completed_monotonic: float | None
    cancel_requested: bool
    reason_code: str | None
    result: Mapping[str, JsonValue]
    effect_state: EffectState = EffectState.NONE

    @property
    def terminal(self) -> bool:
        return self.lifecycle in TERMINAL_LIFECYCLES


@dataclass(frozen=True, slots=True)
class RuntimeView:
    runtime_id: str
    revision: int
    capabilities: tuple[Mapping[str, JsonValue], ...]
    active_motion_operation: str | None
    operations: tuple[OperationSnapshot, ...]
    admission_open: bool
    reconfiguration_slot: str | None
    reconfiguration_generations: Mapping[str, int]
    motion_inhibited_reason: str | None
    request_records: int
    maximum_requests: int
    active_effect_operation: str | None = None
    effect_inhibited_reason: str | None = None


class OperationContext:
    """Handler view of cancellation, deadline, and public physical state."""

    def __init__(
        self,
        operation_id: str,
        deadline_monotonic: float,
        cancel_event: asyncio.Event,
        update: Callable[[str, MotionState], None],
        update_effect: Callable[[EffectState], None],
        physical_effects: bool,
    ) -> None:
        self.operation_id = operation_id
        self.deadline_monotonic = deadline_monotonic
        self._cancel_event = cancel_event
        self._update = update
        self._update_effect = update_effect
        self._physical_effects = physical_effects

    @property
    def cancel_requested(self) -> bool:
        return self._cancel_event.is_set()

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.deadline_monotonic - time.monotonic())

    def report(self, phase: str, motion_state: MotionState) -> None:
        if not phase:
            raise ValueError("operation phase must be nonempty")
        self._update(phase, motion_state)

    def report_effect(self, state: EffectState) -> None:
        """Report native effect knowledge without claiming arm motion or task success."""
        if not isinstance(state, EffectState):
            raise ValueError("effect state must be an EffectState value")
        if not self._physical_effects and state != EffectState.NONE:
            raise ValueError("read-only capability cannot report a physical effect")
        self._update_effect(state)

    async def wait_for_cancel(self) -> None:
        await self._cancel_event.wait()


ValidateArguments = Callable[[Mapping[str, JsonValue]], Mapping[str, object]]
RunCapability = Callable[[OperationContext, Mapping[str, JsonValue]], Awaitable[OperationResult]]


@dataclass(frozen=True, slots=True)
class CapabilitySpec:
    name: str
    version: str
    description: str
    motion_producing: bool
    deadline_seconds: float
    cancel_grace_seconds: float
    validate: ValidateArguments
    run: RunCapability
    input_schema: Mapping[str, object] | None = None
    result_schema: Mapping[str, object] | None = None
    physical_effects: bool = False

    def __post_init__(self) -> None:
        if not self.name or not self.version or not self.description:
            raise ValueError("capability identity and description must be nonempty")
        if not math.isfinite(self.deadline_seconds) or self.deadline_seconds <= 0:
            raise ValueError("capability deadline must be finite and positive")
        if not math.isfinite(self.cancel_grace_seconds) or self.cancel_grace_seconds <= 0:
            raise ValueError("cancel grace must be finite and positive")
        if not isinstance(self.physical_effects, bool):
            raise ValueError("physical effects declaration must be boolean")
        if self.motion_producing:
            object.__setattr__(self, "physical_effects", True)
        for field_name in ("input_schema", "result_schema"):
            schema = getattr(self, field_name)
            if schema is None:
                continue
            try:
                _, checked = _json_object(schema, field_name.replace("_", " "))
            except AdmissionError as error:
                raise ValueError(str(error)) from error
            if checked.get("type") != "object":
                raise ValueError(f"{field_name.replace('_', ' ')} root type must be object")
            object.__setattr__(self, field_name, checked)


@dataclass(slots=True)
class _Operation:
    operation_id: str
    runtime_id: str
    request_id: str
    capability: str
    lifecycle: Lifecycle
    motion_state: MotionState
    phase: str
    accepted_monotonic: float
    deadline_monotonic: float
    completed_monotonic: float | None = None
    reason_code: str | None = None
    result_json: str = "{}"
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    terminal_event: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None
    effect_state: EffectState = EffectState.NONE


@dataclass(frozen=True, slots=True)
class _RequestRecord:
    fingerprint: str
    operation_id: str | None
    reason_code: str | None
    message: str | None


class OperationHost:
    """Single-runtime operation ledger and motion admission authority."""

    def __init__(
        self,
        capabilities: Sequence[CapabilitySpec],
        *,
        runtime_id: str | None = None,
        principal: str = "local",
        maximum_requests: int = 1024,
    ) -> None:
        specs = {item.name: item for item in capabilities}
        if len(specs) != len(capabilities):
            raise ValueError("capability names must be unique")
        if not principal:
            raise ValueError("principal must be nonempty")
        if maximum_requests < 1:
            raise ValueError("maximum requests must be positive")
        self.runtime_id = runtime_id or uuid.uuid4().hex
        self._principal = principal
        self._maximum_requests = maximum_requests
        self._specs = specs
        self._requests: dict[tuple[str, str, str], _RequestRecord] = {}
        self._operations: dict[str, _Operation] = {}
        self._active_motion_operation: str | None = None
        self._active_effect_operation: str | None = None
        self._motion_inhibited_reason: str | None = None
        self._effect_inhibited_reason: str | None = None
        self._reconfiguration_slot: str | None = None
        self._reconfiguration_token: str | None = None
        self._reconfiguration_committed = False
        self._reconfiguration_generations: dict[str, int] = {}
        self._revision = 0
        self._closed = False

    def _snapshot(self, operation: _Operation) -> OperationSnapshot:
        decoded = json.loads(operation.result_json)
        return OperationSnapshot(
            operation_id=operation.operation_id,
            runtime_id=operation.runtime_id,
            request_id=operation.request_id,
            capability=operation.capability,
            lifecycle=operation.lifecycle,
            motion_state=operation.motion_state,
            phase=operation.phase,
            accepted_monotonic=operation.accepted_monotonic,
            deadline_monotonic=operation.deadline_monotonic,
            completed_monotonic=operation.completed_monotonic,
            cancel_requested=operation.cancel_event.is_set(),
            reason_code=operation.reason_code,
            result=cast(Mapping[str, JsonValue], _freeze(decoded)),
            effect_state=operation.effect_state,
        )

    def observe(self) -> RuntimeView:
        catalog_items: list[Mapping[str, JsonValue]] = []
        for spec in sorted(self._specs.values(), key=lambda item: item.name):
            item: dict[str, JsonValue] = {
                "name": spec.name,
                "version": spec.version,
                "description": spec.description,
                "motion_producing": spec.motion_producing,
                "physical_effects": spec.physical_effects,
                "deadline_seconds": spec.deadline_seconds,
            }
            if spec.input_schema is not None:
                item["input_schema"] = cast(JsonValue, _plain(spec.input_schema))
            if spec.result_schema is not None:
                item["result_schema"] = cast(JsonValue, _plain(spec.result_schema))
            catalog_items.append(cast(Mapping[str, JsonValue], _freeze(item)))
        catalog = tuple(catalog_items)
        recent = tuple(self._snapshot(item) for item in tuple(self._operations.values())[-16:])
        return RuntimeView(
            runtime_id=self.runtime_id,
            revision=self._revision,
            capabilities=catalog,
            active_motion_operation=self._active_motion_operation,
            active_effect_operation=self._active_effect_operation,
            operations=recent,
            admission_open=not self._closed and self._reconfiguration_slot is None,
            reconfiguration_slot=self._reconfiguration_slot,
            reconfiguration_generations=MappingProxyType(
                dict(sorted(self._reconfiguration_generations.items()))
            ),
            motion_inhibited_reason=self._motion_inhibited_reason,
            effect_inhibited_reason=self._effect_inhibited_reason,
            request_records=len(self._requests),
            maximum_requests=self._maximum_requests,
        )

    def _remember_rejection(
        self,
        key: tuple[str, str, str],
        fingerprint: str,
        error: AdmissionError,
    ) -> None:
        self._requests[key] = _RequestRecord(
            fingerprint=fingerprint,
            operation_id=None,
            reason_code=error.reason_code,
            message=str(error),
        )

    def _repeat(self, record: _RequestRecord, fingerprint: str) -> OperationSnapshot:
        if record.fingerprint != fingerprint:
            raise AdmissionError(
                "REQUEST_CONFLICT",
                "request ID was already used with different capability arguments",
            )
        if record.operation_id is None:
            raise AdmissionError(
                record.reason_code or "REJECTED",
                record.message or "request was previously rejected",
            )
        return self._snapshot(self._operations[record.operation_id])

    async def start(
        self,
        capability: str,
        arguments: Mapping[str, object],
        *,
        request_id: str,
        expected_runtime_id: str,
    ) -> OperationSnapshot:
        if not request_id:
            raise AdmissionError("INVALID_ARGUMENT", "request ID must be nonempty")
        raw_json, raw_arguments = _json_object(arguments, "capability arguments")
        fingerprint = hashlib.sha256(f"{capability}\0{raw_json}".encode()).hexdigest()
        key = (expected_runtime_id, self._principal, request_id)
        previous = self._requests.get(key)
        if previous is not None:
            return self._repeat(previous, fingerprint)
        if len(self._requests) >= self._maximum_requests:
            raise AdmissionError("REQUEST_LIMIT", "request ledger is full")

        try:
            if self._closed:
                raise AdmissionError("RUNTIME_CLOSED", "runtime is closed")
            if expected_runtime_id != self.runtime_id:
                raise AdmissionError("STALE_RUNTIME", "runtime ID is no longer current")
            if self._reconfiguration_slot is not None:
                raise AdmissionError(
                    "RECONFIGURING",
                    f"runtime is reconfiguring {self._reconfiguration_slot}",
                )
            spec = self._specs.get(capability)
            if spec is None:
                raise AdmissionError("UNKNOWN_CAPABILITY", "capability is not registered")
            try:
                validated = spec.validate(raw_arguments)
            except AdmissionError:
                raise
            except (TypeError, ValueError) as error:
                raise AdmissionError("INVALID_ARGUMENT", str(error)) from error
            _, checked_arguments = _json_object(validated, "validated arguments")
            if spec.motion_producing and self._motion_inhibited_reason is not None:
                raise AdmissionError(
                    "MOTION_INHIBITED",
                    f"motion is inhibited: {self._motion_inhibited_reason}",
                )
            if spec.motion_producing and self._active_motion_operation is not None:
                raise AdmissionError("BUSY", "another motion operation owns the actuator")
            if spec.physical_effects and self._effect_inhibited_reason is not None:
                raise AdmissionError(
                    "EFFECT_INHIBITED",
                    f"physical effects are inhibited: {self._effect_inhibited_reason}",
                )
            if spec.physical_effects and self._active_effect_operation is not None:
                raise AdmissionError("EFFECT_BUSY", "another operation owns physical effects")
        except AdmissionError as error:
            self._remember_rejection(key, fingerprint, error)
            self._revision += 1
            raise

        accepted = time.monotonic()
        operation_id = uuid.uuid4().hex
        operation = _Operation(
            operation_id=operation_id,
            runtime_id=self.runtime_id,
            request_id=request_id,
            capability=capability,
            lifecycle=Lifecycle.ACCEPTED,
            motion_state=MotionState.IDLE,
            phase="admitted",
            accepted_monotonic=accepted,
            deadline_monotonic=accepted + spec.deadline_seconds,
        )
        self._operations[operation_id] = operation
        self._requests[key] = _RequestRecord(fingerprint, operation_id, None, None)
        if spec.motion_producing:
            self._active_motion_operation = operation_id
        if spec.physical_effects:
            self._active_effect_operation = operation_id
        self._revision += 1
        operation.task = asyncio.create_task(
            self._execute(operation, spec, checked_arguments),
            name=f"entryplug:{capability}:{operation_id}",
        )
        return self._snapshot(operation)

    @contextmanager
    def reconfiguration(
        self,
        slot: str,
        *,
        expected_runtime_id: str,
        expected_generation: int | None = None,
    ) -> Iterator[None]:
        """Close admission while an approved runtime slot is replaced at rest."""

        if not slot:
            raise ValueError("reconfiguration slot must be nonempty")
        if self._closed:
            raise AdmissionError("RUNTIME_CLOSED", "runtime is closed")
        if expected_runtime_id != self.runtime_id:
            raise AdmissionError("STALE_RUNTIME", "runtime ID is no longer current")
        current_generation = self._reconfiguration_generations.get(slot, 1)
        if expected_generation is not None and (
            isinstance(expected_generation, bool) or expected_generation != current_generation
        ):
            raise AdmissionError("STALE_GENERATION", "runtime slot generation is no longer current")
        if self._reconfiguration_slot is not None:
            raise AdmissionError(
                "RECONFIGURING",
                f"runtime is already reconfiguring {self._reconfiguration_slot}",
            )
        if any(
            operation.lifecycle not in TERMINAL_LIFECYCLES
            for operation in self._operations.values()
        ):
            raise AdmissionError("BUSY", "runtime still has active operations")
        if self._motion_inhibited_reason is not None:
            raise AdmissionError(
                "PHYSICAL_STATE_UNKNOWN",
                "runtime physical state is not confirmed quiescent",
            )
        if self._effect_inhibited_reason is not None:
            raise AdmissionError(
                "PHYSICAL_STATE_UNKNOWN",
                "runtime physical effect state is not reconciled",
            )

        token = uuid.uuid4().hex
        self._reconfiguration_generations.setdefault(slot, current_generation)
        self._reconfiguration_slot = slot
        self._reconfiguration_token = token
        self._reconfiguration_committed = False
        self._revision += 1
        try:
            yield
        finally:
            if self._reconfiguration_token == token:
                self._reconfiguration_slot = None
                self._reconfiguration_token = None
                self._reconfiguration_committed = False
                self._revision += 1

    def reconfiguration_generation(self, slot: str) -> int:
        if not slot:
            raise ValueError("reconfiguration slot must be nonempty")
        return self._reconfiguration_generations.get(slot, 1)

    def commit_reconfiguration(
        self,
        slot: str,
        *,
        expected_generation: int,
    ) -> int:
        """Commit one prepared instance change under its active barrier."""

        if self._reconfiguration_slot != slot or self._reconfiguration_token is None:
            raise AdmissionError(
                "NO_RECONFIGURATION",
                f"runtime has no active reconfiguration for {slot}",
            )
        current_generation = self._reconfiguration_generations[slot]
        if isinstance(expected_generation, bool) or expected_generation != current_generation:
            raise AdmissionError(
                "STALE_GENERATION",
                "runtime slot generation is no longer current",
            )
        if self._reconfiguration_committed:
            raise AdmissionError(
                "ALREADY_COMMITTED",
                f"runtime reconfiguration for {slot} was already committed",
            )
        generation = current_generation + 1
        self._reconfiguration_generations[slot] = generation
        self._reconfiguration_committed = True
        self._revision += 1
        return generation

    def _update_operation(
        self, operation: _Operation, phase: str, motion_state: MotionState
    ) -> None:
        if operation.lifecycle in TERMINAL_LIFECYCLES:
            return
        operation.phase = phase
        operation.motion_state = motion_state
        self._revision += 1

    def _update_effect(self, operation: _Operation, state: EffectState) -> None:
        if operation.lifecycle in TERMINAL_LIFECYCLES:
            return
        operation.effect_state = state
        self._revision += 1

    async def _deadline(self, operation: _Operation, spec: CapabilitySpec) -> None:
        await asyncio.sleep(max(0.0, operation.deadline_monotonic - time.monotonic()))
        if operation.lifecycle in TERMINAL_LIFECYCLES:
            return
        operation.cancel_event.set()
        operation.lifecycle = Lifecycle.CANCELING
        operation.phase = "stop"
        operation.reason_code = "DEADLINE_EXCEEDED"
        self._revision += 1
        await asyncio.sleep(spec.cancel_grace_seconds)
        if operation.lifecycle not in TERMINAL_LIFECYCLES:
            self._finish(
                operation,
                OperationResult(
                    Lifecycle.INDETERMINATE,
                    MotionState.UNKNOWN if spec.motion_producing else MotionState.IDLE,
                    reason_code="STOP_UNCONFIRMED",
                    effect_state=EffectState.UNKNOWN if spec.physical_effects else EffectState.NONE,
                ),
            )
            if operation.task is not None:
                operation.task.cancel()

    async def _execute(
        self,
        operation: _Operation,
        spec: CapabilitySpec,
        arguments: Mapping[str, JsonValue],
    ) -> None:
        deadline_task = asyncio.create_task(self._deadline(operation, spec))
        if operation.cancel_event.is_set():
            operation.lifecycle = Lifecycle.CANCELING
            operation.phase = "stop"
        else:
            operation.lifecycle = Lifecycle.RUNNING
            operation.phase = "execute"
        self._revision += 1
        context = OperationContext(
            operation.operation_id,
            operation.deadline_monotonic,
            operation.cancel_event,
            lambda phase, motion: self._update_operation(operation, phase, motion),
            lambda state: self._update_effect(operation, state),
            spec.physical_effects,
        )
        try:
            result = await spec.run(context, arguments)
            self._finish(operation, result)
        except asyncio.CancelledError:
            if operation.lifecycle not in TERMINAL_LIFECYCLES:
                self._finish(
                    operation,
                    OperationResult(
                        Lifecycle.INDETERMINATE,
                        MotionState.UNKNOWN if spec.motion_producing else MotionState.IDLE,
                        reason_code="WORKER_CANCELED",
                        effect_state=EffectState.UNKNOWN
                        if spec.physical_effects
                        else EffectState.NONE,
                    ),
                )
        except Exception as error:
            uncertain_motion = operation.motion_state in {
                MotionState.MOVING,
                MotionState.UNKNOWN,
            }
            uncertain_effect = spec.physical_effects and (
                not spec.motion_producing
                or uncertain_motion
                or operation.effect_state != EffectState.NONE
            )
            self._finish(
                operation,
                OperationResult(
                    Lifecycle.INDETERMINATE
                    if uncertain_motion or uncertain_effect
                    else Lifecycle.FAILED,
                    MotionState.UNKNOWN if uncertain_motion else operation.motion_state,
                    result={"error_type": type(error).__name__},
                    reason_code="HANDLER_FAILED",
                    effect_state=EffectState.UNKNOWN if uncertain_effect else EffectState.NONE,
                ),
            )
        finally:
            deadline_task.cancel()

    def _finish(self, operation: _Operation, result: OperationResult) -> None:
        if operation.lifecycle in TERMINAL_LIFECYCLES:
            return
        spec = self._specs[operation.capability]
        if operation.reason_code == "DEADLINE_EXCEEDED" and result.lifecycle in {
            Lifecycle.SUCCEEDED,
            Lifecycle.CANCELED,
        }:
            result = OperationResult(
                Lifecycle.FAILED,
                result.motion_state,
                result=result.result,
                reason_code="DEADLINE_EXCEEDED",
                effect_state=result.effect_state,
            )
        if not spec.physical_effects and result.effect_state != EffectState.NONE:
            raise ValueError("read-only capability cannot return a physical effect")
        result_json, _ = _json_object(result.result, "operation result")
        effect_state = result.effect_state
        if effect_state == EffectState.NONE and operation.effect_state != EffectState.NONE:
            effect_state = (
                EffectState.UNKNOWN
                if operation.effect_state == EffectState.REQUESTED
                else operation.effect_state
            )
        if effect_state == EffectState.REQUESTED or (
            spec.motion_producing and result.motion_state == MotionState.UNKNOWN
        ):
            effect_state = EffectState.UNKNOWN
        lifecycle = result.lifecycle
        reason_code = result.reason_code
        if spec.motion_producing and result.motion_state == MotionState.UNKNOWN:
            lifecycle = Lifecycle.INDETERMINATE
            if result.lifecycle != Lifecycle.INDETERMINATE:
                reason_code = "STOP_UNCONFIRMED"
        elif lifecycle == Lifecycle.SUCCEEDED and effect_state == EffectState.UNKNOWN:
            lifecycle = Lifecycle.INDETERMINATE
            reason_code = "EFFECT_UNCONFIRMED"
        operation.lifecycle = lifecycle
        operation.motion_state = result.motion_state
        operation.effect_state = effect_state
        operation.phase = "terminal"
        operation.reason_code = reason_code
        operation.result_json = result_json
        operation.completed_monotonic = time.monotonic()
        operation.terminal_event.set()
        if spec.motion_producing and result.motion_state == MotionState.UNKNOWN:
            self._motion_inhibited_reason = reason_code or "PHYSICAL_STATE_UNKNOWN"
        if spec.physical_effects and effect_state == EffectState.UNKNOWN:
            self._effect_inhibited_reason = reason_code or "EFFECT_UNCONFIRMED"
        if self._active_motion_operation == operation.operation_id:
            self._active_motion_operation = None
        if self._active_effect_operation == operation.operation_id:
            self._active_effect_operation = None
        self._revision += 1

    def _operation(self, operation_id: str) -> _Operation:
        try:
            return self._operations[operation_id]
        except KeyError as error:
            raise KeyError(f"unknown operation: {operation_id}") from error

    async def wait(self, operation_id: str, timeout_seconds: float) -> OperationSnapshot:
        if not math.isfinite(timeout_seconds) or timeout_seconds < 0:
            raise ValueError("wait timeout must be finite and nonnegative")
        operation = self._operation(operation_id)
        if operation.lifecycle not in TERMINAL_LIFECYCLES:
            try:
                async with asyncio.timeout(timeout_seconds):
                    await operation.terminal_event.wait()
            except TimeoutError:
                pass
        return self._snapshot(operation)

    async def cancel(self, operation_id: str) -> OperationSnapshot:
        operation = self._operation(operation_id)
        if operation.lifecycle not in TERMINAL_LIFECYCLES:
            operation.cancel_event.set()
            operation.lifecycle = Lifecycle.CANCELING
            operation.phase = "stop"
            self._revision += 1
        return self._snapshot(operation)

    async def inspect(self, reference: str, detail: str = "summary") -> Mapping[str, JsonValue]:
        if detail not in {"summary", "result"}:
            raise ValueError("inspect detail must be 'summary' or 'result'")
        snapshot = self._snapshot(self._operation(reference))
        value: dict[str, JsonValue] = {
            "operation_id": snapshot.operation_id,
            "capability": snapshot.capability,
            "lifecycle": snapshot.lifecycle.value,
            "motion_state": snapshot.motion_state.value,
            "effect_state": snapshot.effect_state.value,
            "phase": snapshot.phase,
            "cancel_requested": snapshot.cancel_requested,
            "reason_code": snapshot.reason_code,
        }
        if detail == "result":
            value["result"] = cast(JsonValue, _plain(snapshot.result))
        return cast(Mapping[str, JsonValue], _freeze(value))

    async def close(self) -> tuple[OperationSnapshot, ...]:
        if self._closed:
            return tuple(self._snapshot(item) for item in self._operations.values())
        self._closed = True
        active = [
            item for item in self._operations.values() if item.lifecycle not in TERMINAL_LIFECYCLES
        ]
        for operation in active:
            await self.cancel(operation.operation_id)
        for operation in active:
            spec = self._specs[operation.capability]
            await self.wait(operation.operation_id, spec.cancel_grace_seconds)
            if operation.lifecycle not in TERMINAL_LIFECYCLES:
                self._finish(
                    operation,
                    OperationResult(
                        Lifecycle.INDETERMINATE,
                        MotionState.UNKNOWN if spec.motion_producing else MotionState.IDLE,
                        reason_code="CLOSE_STOP_UNCONFIRMED",
                        effect_state=EffectState.UNKNOWN
                        if spec.physical_effects
                        else EffectState.NONE,
                    ),
                )
                if operation.task is not None:
                    operation.task.cancel()
        self._revision += 1
        return tuple(self._snapshot(item) for item in self._operations.values())
