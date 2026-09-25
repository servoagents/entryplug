"""Drive one live owned Harbor acquisition through A2A HTTP+JSON."""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import socket
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from a2a.client import Client, ClientConfig, ClientFactory
from a2a.client.card_resolver import A2ACardResolver
from a2a.extensions.common import find_extension_by_uri
from a2a.helpers import new_data_part
from a2a.types import (
    AgentCard,
    GetTaskRequest,
    Message,
    Role,
    SendMessageConfiguration,
    SendMessageRequest,
    Task,
    TaskState,
)
from a2a.utils import TransportProtocol
from google.protobuf.json_format import MessageToDict  # type: ignore[import-untyped]

from entryplug.episode import EpisodeFactory
from entryplug.harbor_episode import ACQUIRE_VISUAL_BINDING, harbor_mirrors_episode_factory
from entryplug.pilot import summarize_mirrors_episode
from entryplug.qualification import HARBOR_MIRRORS_V1
from entryplug.runtime import DEFAULT_HARBOR_IMAGE, RUN_ID_PATTERN, RuntimeResult
from entryplug.seeding import validate_experiment_seed
from entryplug_a2a.server import (
    ARTIFACT_NAME,
    CAPABILITY_EXTENSION_URI,
    MEDIA_TYPE,
    create_a2a_server,
)

A2A_PROTOCOL_REVISION = "1.0"
_TERMINAL_STATES = {
    TaskState.TASK_STATE_COMPLETED,
    TaskState.TASK_STATE_FAILED,
    TaskState.TASK_STATE_CANCELED,
    TaskState.TASK_STATE_REJECTED,
}


@dataclass(frozen=True, slots=True)
class A2AHarborResult:
    """Outcome and create only wrapper evidence for one live A2A episode."""

    run_id: str
    evidence_path: Path
    physical_run_id: str
    physical_evidence_path: Path
    passed: bool


def _profile(card: AgentCard) -> dict[str, Any]:
    extension = find_extension_by_uri(card, CAPABILITY_EXTENSION_URI)
    if extension is None:
        raise RuntimeError("A2A Agent Card has no Entryplug capability profile")
    value = MessageToDict(extension.params)
    if not isinstance(value, dict):
        raise RuntimeError("A2A capability profile is not an object")
    return value


def _operation(task: Task) -> dict[str, Any]:
    artifact = next((item for item in task.artifacts if item.name == ARTIFACT_NAME), None)
    if artifact is None or len(artifact.parts) != 1 or not artifact.parts[0].HasField("data"):
        raise RuntimeError("A2A task has no Entryplug operation artifact")
    value = MessageToDict(artifact.parts[0].data)
    if not isinstance(value, dict):
        raise RuntimeError("A2A operation artifact is not an object")
    return value


async def _submit(client: Client, payload: Mapping[str, object]) -> Task:
    events = [
        event
        async for event in client.send_message(
            SendMessageRequest(
                message=Message(
                    role=Role.ROLE_USER,
                    message_id=uuid.uuid4().hex,
                    parts=[new_data_part(dict(payload), media_type=MEDIA_TYPE)],
                ),
                configuration=SendMessageConfiguration(return_immediately=True),
            )
        )
    ]
    if len(events) != 1 or not events[0].HasField("task"):
        raise RuntimeError("A2A did not return one Harbor task")
    return events[0].task


async def _poll_terminal(client: Client, task_id: str) -> tuple[Task, int]:
    for poll_count in range(1, 211):
        task = await client.get_task(GetTaskRequest(id=task_id))
        if task.status.state in _TERMINAL_STATES:
            return task, poll_count
        await asyncio.sleep(1.0)
    raise RuntimeError("A2A Harbor task exceeded 210 bounded polls")


async def _drive_live_harbor(base_url: str, seed: int) -> dict[str, Any]:
    request_id = f"a2a-harbor-{seed}-{HARBOR_MIRRORS_V1.digest.removeprefix('sha256:')}"
    async with httpx.AsyncClient(timeout=15.0) as http:
        card = await A2ACardResolver(http, base_url).get_agent_card()
        profile = _profile(card)
        runtime_id = profile.get("runtimeId")
        capabilities = profile.get("capabilities")
        if not isinstance(runtime_id, str) or not runtime_id:
            raise RuntimeError("A2A capability profile has no runtime ID")
        if not isinstance(capabilities, list) or not any(
            isinstance(item, dict) and item.get("name") == ACQUIRE_VISUAL_BINDING
            for item in capabilities
        ):
            raise RuntimeError("A2A Agent Card does not export Harbor acquisition")

        client = ClientFactory(
            ClientConfig(
                streaming=False,
                polling=False,
                httpx_client=http,
                supported_protocol_bindings=[TransportProtocol.HTTP_JSON],
            )
        ).create(card)
        try:
            request = {
                "capability": ACQUIRE_VISUAL_BINDING,
                "runtime_id": runtime_id,
                "request_id": request_id,
                "arguments": {},
            }
            accepted = await _submit(client, request)
            repeated = await _submit(client, request)
            completed, primary_polls = await _poll_terminal(client, accepted.id)
            repeated_completed, repeated_polls = await _poll_terminal(client, repeated.id)
        finally:
            await client.close()

    operation = _operation(completed)
    repeated_operation = _operation(repeated_completed)
    operation_id = operation.get("operation_id")
    if not isinstance(operation_id, str) or not operation_id:
        raise RuntimeError("A2A task did not return a Harbor operation ID")
    if repeated_operation.get("operation_id") != operation_id:
        raise RuntimeError("A2A did not deduplicate the stable Harbor request")
    if operation.get("request_id") != request_id:
        raise RuntimeError("A2A did not preserve the stable Harbor request ID")
    public_result = operation.get("result")
    if not isinstance(public_result, dict):
        raise RuntimeError("A2A task returned no Harbor operation result")
    return {
        "agent_card": {
            "name": card.name,
            "version": card.version,
            "extension_uri": CAPABILITY_EXTENSION_URI,
            "runtime_id": runtime_id,
            "skills": sorted(skill.name for skill in card.skills),
        },
        "sdk_poll_count": primary_polls + repeated_polls,
        "model_decision_count": 0,
        "deduplicated_request": True,
        "a2a_tasks": [accepted.id, repeated.id],
        "operation": {
            "operation_id": operation_id,
            "runtime_id": runtime_id,
            "request_id": request_id,
            "lifecycle": operation.get("lifecycle"),
            "motion_state": operation.get("motion_state"),
            "reason_code": operation.get("reason_code"),
            "result": public_result,
        },
    }


def _listener() -> tuple[socket.socket, int]:
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    return listener, int(listener.getsockname()[1])


async def _serve_and_drive(factory: EpisodeFactory, seed: int) -> dict[str, Any]:
    listener, port = _listener()
    episode = None
    bundle = None
    server_task: asyncio.Task[None] | None = None
    server: uvicorn.Server | None = None
    try:
        episode = await factory(seed)
        if not episode.session.owns_runtime:
            raise RuntimeError("live A2A qualification requires an owning session")
        base_url = f"http://127.0.0.1:{port}"
        bundle = await create_a2a_server(episode.session, base_url=base_url)
        server = uvicorn.Server(
            uvicorn.Config(
                bundle.app,
                host="127.0.0.1",
                port=port,
                log_level="error",
                lifespan="on",
            )
        )
        server_task = asyncio.create_task(server.serve(sockets=[listener]))
        deadline = time.monotonic() + 5.0
        while not server.started and not server_task.done() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        if not server.started:
            raise RuntimeError("A2A loopback server did not start")
        return await _drive_live_harbor(base_url, seed)
    finally:
        if server is not None:
            server.should_exit = True
        if server_task is not None:
            try:
                await asyncio.wait_for(server_task, timeout=10.0)
            except TimeoutError as error:
                server_task.cancel()
                await asyncio.gather(server_task, return_exceptions=True)
                raise RuntimeError("A2A loopback server did not stop") from error
        else:
            listener.close()
        if bundle is not None:
            await bundle.close()
        if episode is not None:
            await episode.session.close()


def _physical_result(root: Path, transport: Mapping[str, Any]) -> RuntimeResult:
    operation = transport.get("operation")
    result = operation.get("result") if isinstance(operation, Mapping) else None
    if not isinstance(result, Mapping):
        raise ValueError("A2A Harbor result is not an object")
    run_id = result.get("run_id")
    raw_path = result.get("evidence_path")
    if not isinstance(run_id, str) or RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise ValueError("A2A Harbor result has no valid physical run ID")
    if not isinstance(raw_path, str):
        raise ValueError("A2A Harbor result has no physical evidence path")
    runs = (root / "runs").resolve()
    evidence_path = Path(raw_path).resolve()
    expected = (runs / run_id).resolve()
    if evidence_path != expected or not evidence_path.is_relative_to(runs):
        raise ValueError("A2A Harbor evidence resolves outside its declared run")
    return RuntimeResult(0, run_id, evidence_path)


def run_a2a_harbor(
    root: Path,
    *,
    seed: int,
    image: str = DEFAULT_HARBOR_IMAGE,
    episode_factory: EpisodeFactory | None = None,
) -> A2AHarborResult:
    """Run one complete live task through A2A and private evaluation."""

    validate_experiment_seed(seed)
    sdk_version = importlib.metadata.version("a2a-sdk")
    if sdk_version != "1.1.5":
        raise RuntimeError(f"A2A SDK 1.1.5 is required, found {sdk_version}")
    factory = episode_factory or harbor_mirrors_episode_factory(root, image=image)
    started = time.monotonic()
    transport = asyncio.run(_serve_and_drive(factory, seed))

    physical = _physical_result(root, transport)
    evaluation = summarize_mirrors_episode(seed, physical)
    operation = transport["operation"]
    passed = operation["lifecycle"] == "succeeded" and evaluation["outcome"] == "passed"

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"a2a-harbor-s{seed}-{stamp}-{uuid.uuid4().hex[:8]}"
    evidence_path = root / "runs" / run_id
    evidence_path.mkdir(parents=True, exist_ok=False)
    record = {
        "schema_version": 1,
        "recorded_at": datetime.now(UTC).isoformat(),
        "status": "passed" if passed else "failed",
        "experiment_seed": seed,
        "qualification_profile": {
            "profile_id": HARBOR_MIRRORS_V1.profile_id,
            "digest": HARBOR_MIRRORS_V1.digest,
        },
        "physical_run": {
            "case": "mirrors",
            "run_id": physical.run_id,
            "evidence_path": str(physical.evidence_path),
            "container_image": image,
            "shared_launch_path": "entryplug.runtime.harbor_run_command",
            "shared_physical_algorithm": "containers/harbor/mirrors.py",
        },
        "transport": {
            "adapter": "entryplug_a2a",
            "sdk": "a2a-sdk",
            "sdk_version": sdk_version,
            "protocol": "HTTP+JSON",
            "protocol_revision": A2A_PROTOCOL_REVISION,
            "locality": "loopback",
            "elapsed_wall_time_ms": round((time.monotonic() - started) * 1000, 6),
            **transport,
        },
        "private_evaluation": {
            "outcome": evaluation.get("outcome"),
            "selected_role": evaluation.get("selected_role"),
            "selected_controlled_source": evaluation.get("selected_controlled_source"),
            "stale_path_rejected": evaluation.get("stale_path_rejected"),
            "unrelated_source_rejected": evaluation.get("unrelated_source_rejected"),
            "roles_exposed_to_transport": False,
        },
        "claim_boundary": (
            "This run executes the existing Harbor Hall of Mirrors task as one task level "
            "capability through A2A HTTP+JSON. It verifies the same physical launch and "
            "acquisition path; individual physical probes are not separate A2A tasks."
        ),
    }
    with (evidence_path / "a2a.json").open("x", encoding="utf-8") as stream:
        json.dump(record, stream, allow_nan=False, indent=2, sort_keys=True)
        stream.write("\n")
    return A2AHarborResult(
        run_id,
        evidence_path,
        physical.run_id,
        physical.evidence_path,
        passed,
    )
