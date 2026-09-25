"""Isolated, deterministic agent policies over the public session facade."""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import multiprocessing
import os
import time
import uuid
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass, field
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from typing import Protocol, cast

from entryplug.evidence import JsonValue
from entryplug.operation import OperationSnapshot, RuntimeView
from entryplug.session import Session

_MAX_MESSAGE_BYTES = 16_384
_TERMINAL_LIFECYCLES = frozenset({"succeeded", "failed", "canceled", "rejected", "indeterminate"})


class AgentError(RuntimeError):
    """Base error for the isolated policy boundary."""


class AgentBusyError(AgentError):
    """Raised when a second decision is requested concurrently."""


class AgentDecisionTimeout(AgentError):
    """Raised after a policy exceeds its finite decision deadline."""


class AgentStartupTimeout(AgentError):
    """Raised when a new child process does not become ready in time."""


class AgentProcessError(AgentError):
    """Raised when the child process exits or its policy fails."""


class StaleAgentDecision(AgentError):
    """Raised when a decision no longer belongs to the current runtime or agent."""


@dataclass(frozen=True, slots=True)
class Act:
    capability: str
    arguments: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Wait:
    operation_id: str
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class Cancel:
    operation_id: str


@dataclass(frozen=True, slots=True)
class Inspect:
    reference: str
    detail: str = "summary"


@dataclass(frozen=True, slots=True)
class Stop:
    reason: str = "policy complete"


Decision = Act | Wait | Cancel | Inspect | Stop
DecisionResult = OperationSnapshot | Mapping[str, JsonValue] | None


class AgentPolicy(Protocol):
    """A replaceable policy hosted outside the operation process."""

    def decide(self, view: Mapping[str, JsonValue]) -> Decision | Awaitable[Decision]: ...


async def _await_decision(awaitable: Awaitable[Decision]) -> Decision:
    return await awaitable


@dataclass(frozen=True, slots=True)
class DecisionReply:
    decision_id: str
    runtime_id: str
    agent_generation: int
    agent_process_id: int
    process_startup_ms: float | None
    decision_latency_ms: float
    decision: Decision


@dataclass(frozen=True, slots=True)
class AgentStep:
    """One policy decision and the public result of dispatching it."""

    reply: DecisionReply
    result: DecisionResult


async def dispatch_decision(
    session: Session,
    decision: Decision,
    *,
    maximum_wait_seconds: float = 5.0,
    request_id: str | None = None,
) -> DecisionResult:
    """Dispatch one already chosen decision through the shared session facade."""

    if not math.isfinite(maximum_wait_seconds) or maximum_wait_seconds <= 0:
        raise ValueError("maximum wait must be finite and positive")
    if isinstance(decision, Act):
        return await session.act(
            decision.capability,
            decision.arguments,
            request_id=request_id or uuid.uuid4().hex,
        )
    if isinstance(decision, Wait):
        if not math.isfinite(decision.timeout_seconds) or decision.timeout_seconds < 0:
            raise ValueError("wait timeout must be finite and nonnegative")
        if decision.timeout_seconds > maximum_wait_seconds:
            raise AgentError(f"policy wait exceeds {maximum_wait_seconds:g} second limit")
        return await session.wait(decision.operation_id, decision.timeout_seconds)
    if isinstance(decision, Cancel):
        return await session.cancel(decision.operation_id)
    if isinstance(decision, Inspect):
        return await session.inspect(decision.reference, decision.detail)
    return None


def agent_execution_record(
    steps: Sequence[AgentStep],
    operation: OperationSnapshot,
    *,
    policy: str,
) -> dict[str, object]:
    """Build bounded public evidence for one completed agent-driven operation."""

    if not policy:
        raise ValueError("policy name must be nonempty")
    if not steps:
        raise ValueError("agent execution must contain at least one decision")
    if not isinstance(steps[-1].reply.decision, Stop):
        raise ValueError("agent execution must end with a stop decision")
    process_ids = sorted({step.reply.agent_process_id for step in steps})
    if any(process_id == os.getpid() for process_id in process_ids):
        raise ValueError("agent policy did not run in a separate process")

    decisions: list[dict[str, object]] = []
    for step in steps:
        item: dict[str, object] = {
            "decision_id": step.reply.decision_id,
            "kind": type(step.reply.decision).__name__.lower(),
            "agent_generation": step.reply.agent_generation,
            "agent_process_id": step.reply.agent_process_id,
            "process_startup_ms": step.reply.process_startup_ms,
            "decision_latency_ms": step.reply.decision_latency_ms,
        }
        if isinstance(step.result, OperationSnapshot):
            item["operation_lifecycle"] = step.result.lifecycle.value
            item["operation_phase"] = step.result.phase
        decisions.append(item)

    return {
        "status": "passed",
        "policy": policy,
        "policy_process_ids": process_ids,
        "operation_process_id": os.getpid(),
        "separate_policy_process": True,
        "decision_count": len(steps),
        "wait_decision_count": sum(isinstance(step.reply.decision, Wait) for step in steps),
        "decisions": decisions,
        "operation": {
            "operation_id": operation.operation_id,
            "request_id": operation.request_id,
            "runtime_id": operation.runtime_id,
            "capability": operation.capability,
            "lifecycle": operation.lifecycle.value,
            "motion_state": operation.motion_state.value,
            "reason_code": operation.reason_code,
        },
        "claim_boundary": (
            "The child policy selected public session decisions. Only the operation "
            "host admitted work, generated mutation IDs, and recorded terminal results."
        ),
    }


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _json_object(value: Mapping[str, object], label: str) -> dict[str, JsonValue]:
    try:
        encoded = json.dumps(
            _plain(value),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must be finite JSON data") from error
    if len(encoded.encode()) > _MAX_MESSAGE_BYTES:
        raise ValueError(f"{label} exceeds {_MAX_MESSAGE_BYTES} bytes")
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):
        raise ValueError(f"{label} must be an object")
    return cast(dict[str, JsonValue], decoded)


def public_view(view: RuntimeView) -> dict[str, JsonValue]:
    """Convert a host snapshot to the bounded, JSON-safe view policies receive."""

    operations: list[JsonValue] = []
    for operation in view.operations:
        operations.append(
            {
                "operation_id": operation.operation_id,
                "capability": operation.capability,
                "lifecycle": operation.lifecycle.value,
                "motion_state": operation.motion_state.value,
                "phase": operation.phase,
                "cancel_requested": operation.cancel_requested,
                "reason_code": operation.reason_code,
                "result": cast(JsonValue, _plain(operation.result)),
            }
        )
    raw: dict[str, object] = {
        "runtime_id": view.runtime_id,
        "revision": view.revision,
        "capabilities": [_plain(item) for item in view.capabilities],
        "active_motion_operation": view.active_motion_operation,
        "operations": operations,
        "admission_open": view.admission_open,
        "reconfiguration_slot": view.reconfiguration_slot,
        "reconfiguration_generations": _plain(view.reconfiguration_generations),
        "motion_inhibited_reason": view.motion_inhibited_reason,
        "request_capacity": {
            "used": view.request_records,
            "maximum": view.maximum_requests,
        },
    }
    return _json_object(raw, "agent view")


def _nonempty(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a nonempty string")
    return value


def _encode_decision(decision: Decision) -> dict[str, JsonValue]:
    if isinstance(decision, Act):
        return {
            "kind": "act",
            "capability": _nonempty(decision.capability, "capability"),
            "arguments": _json_object(decision.arguments, "action arguments"),
        }
    if isinstance(decision, Wait):
        if not math.isfinite(decision.timeout_seconds) or decision.timeout_seconds < 0:
            raise ValueError("wait timeout must be finite and nonnegative")
        return {
            "kind": "wait",
            "operation_id": _nonempty(decision.operation_id, "operation ID"),
            "timeout_seconds": decision.timeout_seconds,
        }
    if isinstance(decision, Cancel):
        return {
            "kind": "cancel",
            "operation_id": _nonempty(decision.operation_id, "operation ID"),
        }
    if isinstance(decision, Inspect):
        if decision.detail not in {"summary", "result"}:
            raise ValueError("inspection detail must be 'summary' or 'result'")
        return {
            "kind": "inspect",
            "reference": _nonempty(decision.reference, "inspection reference"),
            "detail": decision.detail,
        }
    if isinstance(decision, Stop):
        return {"kind": "stop", "reason": _nonempty(decision.reason, "stop reason")}
    raise TypeError("policy returned an unsupported decision")


def _decode_decision(value: object) -> Decision:
    if not isinstance(value, dict):
        raise AgentProcessError("agent returned a malformed decision")
    kind = value.get("kind")
    if kind == "act":
        arguments = value.get("arguments")
        if not isinstance(arguments, dict):
            raise AgentProcessError("agent returned malformed action arguments")
        return Act(_nonempty(value.get("capability"), "capability"), arguments)
    if kind == "wait":
        timeout = value.get("timeout_seconds")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise AgentProcessError("agent returned a malformed wait timeout")
        return Wait(
            _nonempty(value.get("operation_id"), "operation ID"),
            float(timeout),
        )
    if kind == "cancel":
        return Cancel(_nonempty(value.get("operation_id"), "operation ID"))
    if kind == "inspect":
        detail = value.get("detail")
        if detail not in {"summary", "result"}:
            raise AgentProcessError("agent returned a malformed inspection detail")
        return Inspect(
            _nonempty(value.get("reference"), "inspection reference"),
            cast(str, detail),
        )
    if kind == "stop":
        return Stop(_nonempty(value.get("reason"), "stop reason"))
    raise AgentProcessError("agent returned an unknown decision kind")


def _agent_worker(connection: Connection, policy: AgentPolicy, generation: int) -> None:
    try:
        connection.send(
            {
                "kind": "ready",
                "generation": generation,
                "process_id": os.getpid(),
            }
        )
        while True:
            request = connection.recv()
            if not isinstance(request, dict):
                continue
            if request.get("kind") == "close":
                return
            if request.get("kind") != "decide":
                continue
            decision_id = request.get("decision_id")
            runtime_id = request.get("runtime_id")
            view = request.get("view")
            try:
                if not isinstance(view, dict):
                    raise ValueError("agent view is malformed")
                decision_or_awaitable = policy.decide(cast(Mapping[str, JsonValue], view))
                if inspect.isawaitable(decision_or_awaitable):
                    decision: Decision = asyncio.run(_await_decision(decision_or_awaitable))
                else:
                    decision = decision_or_awaitable
                encoded = _json_object(_encode_decision(decision), "agent decision")
                connection.send(
                    {
                        "ok": True,
                        "decision_id": decision_id,
                        "runtime_id": runtime_id,
                        "generation": generation,
                        "process_id": os.getpid(),
                        "decision": encoded,
                    }
                )
            except Exception as error:
                connection.send(
                    {
                        "ok": False,
                        "decision_id": decision_id,
                        "runtime_id": runtime_id,
                        "generation": generation,
                        "process_id": os.getpid(),
                        "error_type": type(error).__name__,
                        "message": str(error)[:500],
                    }
                )
    except (EOFError, BrokenPipeError, OSError):
        return
    finally:
        connection.close()


async def _receive(connection: Connection, timeout_seconds: float) -> object | None:
    """Poll without blocking the event loop that owns admitted operations."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while not connection.poll(0):
        remaining = deadline - loop.time()
        if remaining <= 0:
            return None
        await asyncio.sleep(min(0.005, remaining))
    return cast(object, connection.recv())


class AgentRunner:
    """Run one policy process and dispatch its decisions through a Session."""

    def __init__(
        self,
        policy: AgentPolicy,
        *,
        decision_timeout_seconds: float = 2.0,
        startup_timeout_seconds: float = 10.0,
        maximum_wait_seconds: float = 5.0,
    ) -> None:
        if not math.isfinite(decision_timeout_seconds) or decision_timeout_seconds <= 0:
            raise ValueError("decision timeout must be finite and positive")
        if not math.isfinite(startup_timeout_seconds) or startup_timeout_seconds <= 0:
            raise ValueError("startup timeout must be finite and positive")
        if not math.isfinite(maximum_wait_seconds) or maximum_wait_seconds <= 0:
            raise ValueError("maximum wait must be finite and positive")
        self._policy = policy
        self._decision_timeout_seconds = decision_timeout_seconds
        self._startup_timeout_seconds = startup_timeout_seconds
        self._maximum_wait_seconds = maximum_wait_seconds
        self._generation = 1
        self._process: BaseProcess | None = None
        self._connection: Connection | None = None
        self._decision_in_flight = False
        self._closed = False

    @property
    def generation(self) -> int:
        return self._generation

    def _start_worker(self) -> tuple[BaseProcess, Connection, bool]:
        if self._process is not None and self._connection is not None:
            return self._process, self._connection, False
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe(duplex=True)
        process = context.Process(
            target=_agent_worker,
            args=(child, self._policy, self._generation),
            name=f"entryplug-agent-{self._generation}",
            daemon=True,
        )
        try:
            process.start()
        except Exception:
            parent.close()
            child.close()
            raise
        child.close()
        self._process = process
        self._connection = parent
        return process, parent, True

    def _detach_worker(self) -> tuple[BaseProcess | None, Connection | None]:
        process, connection = self._process, self._connection
        self._process = None
        self._connection = None
        return process, connection

    def _invalidate_worker(self) -> None:
        retired = self._detach_worker()
        self._generation += 1
        self._stop_worker(*retired, force=True)

    @staticmethod
    def _stop_worker(
        process: BaseProcess | None,
        connection: Connection | None,
        *,
        force: bool,
    ) -> None:
        if connection is not None and not force:
            try:
                connection.send({"kind": "close"})
            except (BrokenPipeError, EOFError, OSError):
                pass
        if process is not None:
            process.join(0.2)
            if process.is_alive():
                process.terminate()
                process.join(0.5)
            if process.is_alive():
                process.kill()
                process.join(0.5)
            process.close()
        if connection is not None:
            connection.close()

    async def decide(self, view: RuntimeView) -> DecisionReply:
        if self._closed:
            raise AgentError("agent runner is closed")
        if self._decision_in_flight:
            raise AgentBusyError("one agent decision is already outstanding")
        self._decision_in_flight = True
        generation = self._generation
        decision_id = uuid.uuid4().hex
        startup_started = time.monotonic()
        process_startup_ms: float | None = None
        try:
            process, connection, started = self._start_worker()
            request = {
                "kind": "decide",
                "decision_id": decision_id,
                "runtime_id": view.runtime_id,
                "generation": generation,
                "view": public_view(view),
            }
            try:
                if started:
                    ready = await _receive(connection, self._startup_timeout_seconds)
                    if ready is None:
                        self._invalidate_worker()
                        raise AgentStartupTimeout(
                            f"agent process startup exceeded {self._startup_timeout_seconds:g} "
                            "seconds"
                        )
                    if (
                        not isinstance(ready, dict)
                        or ready.get("kind") != "ready"
                        or ready.get("generation") != generation
                        or ready.get("process_id") != process.pid
                    ):
                        self._invalidate_worker()
                        raise AgentProcessError("agent returned a malformed startup response")
                    process_startup_ms = round((time.monotonic() - startup_started) * 1000, 3)
                decision_started = time.monotonic()
                connection.send(request)
                response = await _receive(
                    connection,
                    self._decision_timeout_seconds,
                )
            except (EOFError, BrokenPipeError, OSError) as error:
                exit_code = process.exitcode
                self._invalidate_worker()
                raise AgentProcessError(f"agent process exited with code {exit_code}") from error
            if response is None:
                self._invalidate_worker()
                raise AgentDecisionTimeout(
                    f"agent decision exceeded {self._decision_timeout_seconds:g} seconds"
                )
            if not isinstance(response, dict):
                self._invalidate_worker()
                raise AgentProcessError("agent returned a malformed response")
            if (
                response.get("decision_id") != decision_id
                or response.get("runtime_id") != view.runtime_id
                or response.get("generation") != generation
                or not isinstance(response.get("process_id"), int)
                or generation != self._generation
            ):
                self._invalidate_worker()
                raise StaleAgentDecision("discarded a decision from an old runtime or agent")
            if response.get("ok") is not True:
                error_type = response.get("error_type", "PolicyError")
                message = response.get("message", "policy failed")
                raise AgentProcessError(f"{error_type}: {message}")
            return DecisionReply(
                decision_id=decision_id,
                runtime_id=view.runtime_id,
                agent_generation=generation,
                agent_process_id=cast(int, response["process_id"]),
                process_startup_ms=process_startup_ms,
                decision_latency_ms=round((time.monotonic() - decision_started) * 1000, 3),
                decision=_decode_decision(response.get("decision")),
            )
        except asyncio.CancelledError:
            self._invalidate_worker()
            raise
        finally:
            self._decision_in_flight = False

    async def step(self, session: Session) -> AgentStep:
        view = await session.observe()
        reply = await self.decide(view)
        current = await session.observe()
        if current.runtime_id != reply.runtime_id or reply.agent_generation != self._generation:
            raise StaleAgentDecision("discarded a decision from an old runtime or agent")

        result = await dispatch_decision(
            session,
            reply.decision,
            maximum_wait_seconds=self._maximum_wait_seconds,
        )
        return AgentStep(reply, result)

    async def run_until_stop(
        self,
        session: Session,
        *,
        maximum_decisions: int = 32,
    ) -> tuple[AgentStep, ...]:
        if maximum_decisions < 1:
            raise ValueError("maximum decisions must be positive")
        steps: list[AgentStep] = []
        for _ in range(maximum_decisions):
            step = await self.step(session)
            steps.append(step)
            if isinstance(step.reply.decision, Stop):
                return tuple(steps)
        raise AgentError("policy exceeded the maximum decision count")

    async def replace(self, policy: AgentPolicy) -> None:
        """Replace an idle policy without transferring hidden process state."""

        if self._closed:
            raise AgentError("agent runner is closed")
        if self._decision_in_flight:
            raise AgentBusyError("cannot replace an agent with a decision outstanding")
        retired = self._detach_worker()
        self._stop_worker(*retired, force=False)
        self._policy = policy
        self._generation += 1

    async def close(self) -> None:
        if self._closed:
            return
        if self._decision_in_flight:
            raise AgentBusyError("cannot close an agent with a decision outstanding")
        self._closed = True
        retired = self._detach_worker()
        self._stop_worker(*retired, force=False)

    async def __aenter__(self) -> AgentRunner:
        if self._closed:
            raise AgentError("agent runner is closed")
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()


class ScriptedExplorer:
    """A free policy that submits one configured capability and waits for its result."""

    def __init__(
        self,
        capability: str,
        arguments: Mapping[str, object],
        *,
        wait_seconds: float = 0.25,
    ) -> None:
        self._capability = _nonempty(capability, "capability")
        self._arguments = _json_object(arguments, "action arguments")
        if not math.isfinite(wait_seconds) or wait_seconds <= 0:
            raise ValueError("scripted wait must be finite and positive")
        self._wait_seconds = wait_seconds
        self._submitted = False
        self._known_operations: set[str] = set()

    def decide(self, view: Mapping[str, JsonValue]) -> Decision:
        operations = view.get("operations")
        if not isinstance(operations, list):
            raise ValueError("public view has no operation list")
        if not self._submitted:
            self._known_operations = {
                str(item["operation_id"])
                for item in operations
                if isinstance(item, dict) and isinstance(item.get("operation_id"), str)
            }
            self._submitted = True
            return Act(self._capability, self._arguments)

        candidates = [
            item
            for item in operations
            if isinstance(item, dict)
            and item.get("capability") == self._capability
            and item.get("operation_id") not in self._known_operations
        ]
        if not candidates:
            return Stop("submitted operation is absent from the public view")
        operation = candidates[-1]
        operation_id = _nonempty(operation.get("operation_id"), "operation ID")
        lifecycle = operation.get("lifecycle")
        if lifecycle in _TERMINAL_LIFECYCLES:
            return Stop(f"{self._capability} finished with {lifecycle}")
        return Wait(operation_id, self._wait_seconds)


class CatalogInspectingExplorer:
    """Choose an offered configured capability, then inspect its terminal result."""

    def __init__(
        self,
        options: Mapping[str, Mapping[str, object]],
        *,
        wait_seconds: float = 0.25,
    ) -> None:
        if not options:
            raise ValueError("catalog explorer requires at least one capability option")
        self._options = {
            _nonempty(name, "capability"): _json_object(arguments, "action arguments")
            for name, arguments in options.items()
        }
        if not math.isfinite(wait_seconds) or wait_seconds <= 0:
            raise ValueError("catalog wait must be finite and positive")
        self._wait_seconds = wait_seconds
        self._capability: str | None = None
        self._known_operations: set[str] = set()
        self._inspection_requested = False

    def decide(self, view: Mapping[str, JsonValue]) -> Decision:
        operations = view.get("operations")
        capabilities = view.get("capabilities")
        if not isinstance(operations, list) or not isinstance(capabilities, list):
            raise ValueError("public view has no operation or capability catalog")

        if self._capability is None:
            offered = {
                str(item["name"])
                for item in capabilities
                if isinstance(item, dict) and isinstance(item.get("name"), str)
            }
            matches = sorted(offered.intersection(self._options))
            if not matches:
                return Stop("no configured capability is offered")
            self._capability = matches[0]
            self._known_operations = {
                str(item["operation_id"])
                for item in operations
                if isinstance(item, dict) and isinstance(item.get("operation_id"), str)
            }
            return Act(self._capability, self._options[self._capability])

        candidates = [
            item
            for item in operations
            if isinstance(item, dict)
            and item.get("capability") == self._capability
            and item.get("operation_id") not in self._known_operations
        ]
        if not candidates:
            return Stop("submitted catalog operation is absent from the public view")
        operation = candidates[-1]
        operation_id = _nonempty(operation.get("operation_id"), "operation ID")
        lifecycle = operation.get("lifecycle")
        if lifecycle not in _TERMINAL_LIFECYCLES:
            return Wait(operation_id, self._wait_seconds)
        if not self._inspection_requested:
            self._inspection_requested = True
            return Inspect(operation_id, "result")
        return Stop(f"inspected {self._capability} result after {lifecycle}")
