"""A2A tasks backed by Entryplug's shared operation authority."""

from __future__ import annotations

import json
import math
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import cast
from urllib.parse import urlsplit

from a2a.helpers import new_data_part, new_task_from_user_message
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler, DefaultRequestHandlerV2
from a2a.server.routes import create_agent_card_routes, create_rest_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill, Message, Task
from a2a.utils import TransportProtocol
from a2a.utils.errors import InvalidParamsError
from google.protobuf.json_format import MessageToDict  # type: ignore[import-untyped]
from starlette.applications import Starlette

from entryplug.evidence import JsonValue
from entryplug.operation import AdmissionError, Lifecycle, MotionState, OperationSnapshot
from entryplug.session import Session

MEDIA_TYPE = "application/json"
ARTIFACT_NAME = "entryplug-operation"
_REQUEST_FIELDS = frozenset({"capability", "runtime_id", "request_id", "arguments"})
_MAX_SAFE_INTEGER = 2**53 - 1


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _protobuf_json(value: object) -> object:
    """Restore JSON integers represented as protobuf double values."""

    if isinstance(value, dict):
        return {str(key): _protobuf_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_protobuf_json(item) for item in value]
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        if abs(value) > _MAX_SAFE_INTEGER:
            raise ValueError("integer exceeds the exact A2A structured data range")
        return int(value)
    return value


def _operation_payload(snapshot: OperationSnapshot) -> dict[str, object]:
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
        "result": _plain(snapshot.result),
    }


def _error_payload(reason_code: str, message: str) -> dict[str, object]:
    return {"error": {"reason_code": reason_code, "message": message}}


def _required_text(value: Mapping[str, object], field: str) -> str:
    item = value.get(field)
    if not isinstance(item, str) or not item:
        raise ValueError(f"{field} must be a nonempty string")
    return item


def _request(message: Message | None) -> tuple[str, str, str, Mapping[str, object]]:
    if message is None or len(message.parts) != 1:
        raise ValueError("message must contain exactly one structured data part")
    part = message.parts[0]
    if not part.HasField("data") or part.media_type != MEDIA_TYPE:
        raise ValueError("message part must be structured application/json data")
    decoded = _protobuf_json(MessageToDict(part.data))
    if not isinstance(decoded, dict):
        raise ValueError("message data must be an object")
    envelope = cast(dict[str, object], decoded)
    if set(envelope) != _REQUEST_FIELDS:
        raise ValueError(
            "message data fields must be capability, runtime_id, request_id, and arguments"
        )
    arguments = envelope["arguments"]
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")
    json.dumps(envelope, allow_nan=False, ensure_ascii=False)
    return (
        _required_text(envelope, "capability"),
        _required_text(envelope, "runtime_id"),
        _required_text(envelope, "request_id"),
        cast(Mapping[str, object], arguments),
    )


def _task(context: RequestContext) -> Task:
    if context.current_task is not None:
        return context.current_task
    if context.message is None:
        raise InvalidParamsError(message="A2A request has no message")
    return new_task_from_user_message(context.message)


@dataclass(slots=True)
class _Binding:
    operation_id: str
    canceling: bool = False


class EntryplugAgentExecutor(AgentExecutor):
    """Translate A2A task lifecycle events to one Entryplug operation ledger."""

    def __init__(self, session: Session, exported_capabilities: frozenset[str]) -> None:
        self._session = session
        self._exported_capabilities = exported_capabilities
        self._bindings: dict[str, _Binding] = {}

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = _task(context)
        try:
            capability, runtime_id, request_id, arguments = _request(context.message)
            if capability not in self._exported_capabilities:
                raise AdmissionError("UNKNOWN_CAPABILITY", "capability is not exported")
            operation = await self._session.act(
                capability,
                arguments,
                request_id=request_id,
                expected_runtime_id=runtime_id,
            )
            binding = _Binding(operation.operation_id)
            self._bindings[task.id] = binding
        except AdmissionError as error:
            await event_queue.enqueue_event(task)
            await self._reject(task, event_queue, error.reason_code, str(error))
            return
        except (TypeError, ValueError) as error:
            await event_queue.enqueue_event(task)
            await self._reject(task, event_queue, "INVALID_ARGUMENT", str(error))
            return

        await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task.id, task.context_id)
        await updater.start_work()
        operation = await self._wait_terminal(operation)
        if binding.canceling:
            return
        await self._publish_terminal(updater, operation)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id = context.task_id
        if not task_id or task_id not in self._bindings:
            raise InvalidParamsError(message="task has no admitted Entryplug operation")
        binding = self._bindings[task_id]
        binding.canceling = True
        try:
            operation = await self._session.cancel(binding.operation_id)
        except KeyError as error:
            raise InvalidParamsError(
                message="Entryplug operation is no longer available"
            ) from error
        operation = await self._wait_terminal(operation)
        task = context.current_task
        if task is None:
            raise InvalidParamsError(message="A2A task is no longer available")
        updater = TaskUpdater(event_queue, task.id, task.context_id)
        await self._publish_terminal(updater, operation)

    async def _wait_terminal(self, operation: OperationSnapshot) -> OperationSnapshot:
        current = operation
        while not current.terminal:
            current = await self._session.wait(current, 1.0)
        return current

    @staticmethod
    async def _reject(
        task: Task,
        event_queue: EventQueue,
        reason_code: str,
        message: str,
    ) -> None:
        updater = TaskUpdater(event_queue, task.id, task.context_id)
        await updater.add_artifact(
            [new_data_part(_error_payload(reason_code, message), media_type=MEDIA_TYPE)],
            name=ARTIFACT_NAME,
        )
        await updater.reject()

    @staticmethod
    async def _publish_terminal(updater: TaskUpdater, operation: OperationSnapshot) -> None:
        await updater.add_artifact(
            [new_data_part(_operation_payload(operation), media_type=MEDIA_TYPE)],
            name=ARTIFACT_NAME,
        )
        if operation.lifecycle == Lifecycle.SUCCEEDED:
            await updater.complete()
        elif operation.lifecycle == Lifecycle.CANCELED and operation.motion_state in {
            MotionState.IDLE,
            MotionState.HOLDING,
        }:
            await updater.cancel()
        elif operation.lifecycle == Lifecycle.REJECTED:
            await updater.reject()
        else:
            await updater.failed()


@dataclass(slots=True)
class A2AServer:
    """Composed A2A application. The caller retains ownership of the session."""

    app: Starlette
    card: AgentCard
    executor: EntryplugAgentExecutor
    handler: DefaultRequestHandlerV2
    task_store: InMemoryTaskStore
    _closed: bool = False

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.handler.aclose()


def _base_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("A2A base URL must be an absolute HTTP or HTTPS URL")
    if parsed.query or parsed.fragment:
        raise ValueError("A2A base URL cannot contain a query or fragment")
    return value.rstrip("/")


def _skill(capability: Mapping[str, JsonValue]) -> AgentSkill | None:
    schema = capability.get("input_schema")
    name = capability.get("name")
    version = capability.get("version")
    description = capability.get("description")
    motion = capability.get("motion_producing")
    if not isinstance(schema, Mapping):
        return None
    if (
        not isinstance(name, str)
        or not isinstance(version, str)
        or not isinstance(description, str)
    ):
        raise ValueError("capability catalog entry has no stable identity")
    if not isinstance(motion, bool):
        raise ValueError("capability motion classification is missing")
    request_schema = json.dumps(_plain(schema), separators=(",", ":"), sort_keys=True)
    return AgentSkill(
        id=f"entryplug.{name}",
        name=name,
        description=f"{description} Input schema: {request_schema}",
        tags=["entryplug-capability", "motion" if motion else "read-only", f"version-{version}"],
        input_modes=[MEDIA_TYPE],
        output_modes=[MEDIA_TYPE],
    )


async def create_a2a_server(session: Session, *, base_url: str) -> A2AServer:
    """Create an A2A 1.0 HTTP+JSON server for a live Entryplug session."""

    url = _base_url(base_url)
    view = await session.observe()
    skills: list[AgentSkill] = []
    for capability in view.capabilities:
        skill = _skill(capability)
        if skill is not None:
            skills.append(skill)
    skills.sort(key=lambda item: item.name)
    exported = frozenset(skill.name for skill in skills)
    card = AgentCard(
        name="Entryplug",
        description=(
            "Agent first physical capability runtime. Submit one structured capability request; "
            f"current runtime ID: {view.runtime_id}."
        ),
        version="0.0.1",
        capabilities=AgentCapabilities(streaming=False, push_notifications=False),
        default_input_modes=[MEDIA_TYPE],
        default_output_modes=[MEDIA_TYPE],
        supported_interfaces=[
            AgentInterface(
                url=url,
                protocol_binding=TransportProtocol.HTTP_JSON,
                protocol_version="1.0",
            )
        ],
        skills=skills,
    )
    executor = EntryplugAgentExecutor(session, exported)
    task_store = InMemoryTaskStore()
    handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=task_store,
        agent_card=card,
    )

    server: A2AServer | None = None

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if server is not None:
                await server.close()

    app = Starlette(
        routes=[
            *create_agent_card_routes(agent_card=card),
            *create_rest_routes(request_handler=handler),
        ],
        lifespan=lifespan,
    )
    server = A2AServer(app, card, executor, handler, task_store)
    return server
