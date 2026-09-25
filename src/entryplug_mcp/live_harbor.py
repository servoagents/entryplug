"""Drive one live owned Harbor acquisition through Streamable HTTP MCP."""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import time
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp import Client
from mcp.server.lowlevel import Server

from entryplug.episode import EpisodeFactory
from entryplug.harbor_episode import ACQUIRE_VISUAL_BINDING, harbor_mirrors_episode_factory
from entryplug.pilot import summarize_mirrors_episode
from entryplug.qualification import HARBOR_MIRRORS_V1
from entryplug.runtime import DEFAULT_HARBOR_IMAGE, RUN_ID_PATTERN, RuntimeResult
from entryplug.seeding import validate_experiment_seed
from entryplug.session import Session
from entryplug_mcp.loopback import start_loopback_server
from entryplug_mcp.server import CATALOG_TOOL, INSPECT_TOOL, create_mcp_server

TERMINAL_LIFECYCLES = {
    "succeeded",
    "failed",
    "canceled",
    "rejected",
    "indeterminate",
}
MCP_PROTOCOL_REVISION = "2026-07-28"


@dataclass(frozen=True, slots=True)
class McpHarborResult:
    """Outcome and create only wrapper evidence for one live MCP episode."""

    run_id: str
    evidence_path: Path
    physical_run_id: str
    physical_evidence_path: Path
    passed: bool


def _mcp_server(factory: EpisodeFactory, seed: int) -> Server[Session]:
    @asynccontextmanager
    async def lifespan(_: Server[Session]) -> AsyncIterator[Session]:
        episode = await factory(seed)
        try:
            yield episode.session
        finally:
            await episode.session.close()

    return create_mcp_server(lifespan)


async def _drive_live_harbor(port: int, seed: int) -> dict[str, Any]:
    request_id = f"mcp-harbor-{seed}-{HARBOR_MIRRORS_V1.digest.removeprefix('sha256:')}"
    async with Client(f"http://127.0.0.1:{port}/mcp") as client:
        catalog = await client.call_tool(CATALOG_TOOL, {})
        if catalog.is_error or not isinstance(catalog.structured_content, dict):
            raise RuntimeError("MCP did not return the Entryplug capability catalog")
        runtime_id = catalog.structured_content.get("runtime_id")
        if not isinstance(runtime_id, str) or not runtime_id:
            raise RuntimeError("MCP catalog did not return a runtime ID")

        arguments = {"runtime_id": runtime_id, "request_id": request_id}
        accepted = await client.call_tool(ACQUIRE_VISUAL_BINDING, arguments)
        repeated = await client.call_tool(ACQUIRE_VISUAL_BINDING, arguments)
        if accepted.is_error or not isinstance(accepted.structured_content, dict):
            raise RuntimeError("MCP rejected the Harbor acquisition")
        if repeated.is_error or not isinstance(repeated.structured_content, dict):
            raise RuntimeError("MCP rejected the repeated Harbor acquisition request")
        operation_id = accepted.structured_content.get("operation_id")
        if not isinstance(operation_id, str) or not operation_id:
            raise RuntimeError("MCP did not return a Harbor operation ID")
        if repeated.structured_content.get("operation_id") != operation_id:
            raise RuntimeError("MCP did not deduplicate the stable Harbor request")
        if accepted.structured_content.get("request_id") != request_id:
            raise RuntimeError("MCP did not preserve the stable Harbor request ID")

        poll_count = 0
        inspected_payload: dict[str, Any] | None = None
        while poll_count < 210:
            inspected = await client.call_tool(
                INSPECT_TOOL,
                {
                    "runtime_id": runtime_id,
                    "operation_id": operation_id,
                    "detail": "result",
                },
            )
            poll_count += 1
            if inspected.is_error or not isinstance(inspected.structured_content, dict):
                raise RuntimeError("MCP could not inspect the Harbor operation")
            inspected_payload = inspected.structured_content
            if inspected_payload.get("lifecycle") in TERMINAL_LIFECYCLES:
                break
            await asyncio.sleep(1.0)
        if (
            inspected_payload is None
            or inspected_payload.get("lifecycle") not in TERMINAL_LIFECYCLES
        ):
            raise RuntimeError("MCP Harbor operation exceeded 210 bounded polls")

        final_catalog = await client.call_tool(CATALOG_TOOL, {})
        if final_catalog.is_error or not isinstance(final_catalog.structured_content, dict):
            raise RuntimeError("MCP did not return final admission state")

    public_result = inspected_payload.get("result")
    if not isinstance(public_result, dict):
        raise RuntimeError("MCP inspection returned no Harbor operation result")
    return {
        "initial_catalog": catalog.structured_content,
        "final_admission": {
            "active_motion_operation": final_catalog.structured_content.get(
                "active_motion_operation"
            ),
            "admission_open": final_catalog.structured_content.get("admission_open"),
            "motion_inhibited_reason": final_catalog.structured_content.get(
                "motion_inhibited_reason"
            ),
        },
        "sdk_poll_count": poll_count,
        "model_decision_count": 0,
        "deduplicated_request": True,
        "operation": {
            "operation_id": operation_id,
            "runtime_id": runtime_id,
            "request_id": request_id,
            "lifecycle": inspected_payload.get("lifecycle"),
            "motion_state": inspected_payload.get("motion_state"),
            "reason_code": inspected_payload.get("reason_code"),
            "result": public_result,
        },
    }


def _physical_result(root: Path, transport: Mapping[str, Any]) -> RuntimeResult:
    operation = transport.get("operation")
    result = operation.get("result") if isinstance(operation, Mapping) else None
    if not isinstance(result, Mapping):
        raise ValueError("MCP Harbor result is not an object")
    run_id = result.get("run_id")
    raw_path = result.get("evidence_path")
    if not isinstance(run_id, str) or RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise ValueError("MCP Harbor result has no valid physical run ID")
    if not isinstance(raw_path, str):
        raise ValueError("MCP Harbor result has no physical evidence path")
    runs = (root / "runs").resolve()
    evidence_path = Path(raw_path).resolve()
    expected = (runs / run_id).resolve()
    if evidence_path != expected or not evidence_path.is_relative_to(runs):
        raise ValueError("MCP Harbor evidence resolves outside its declared run")
    return RuntimeResult(0, run_id, evidence_path)


def run_mcp_harbor(
    root: Path,
    *,
    seed: int,
    image: str = DEFAULT_HARBOR_IMAGE,
    episode_factory: EpisodeFactory | None = None,
) -> McpHarborResult:
    """Run one complete live task through MCP and private evaluation."""

    validate_experiment_seed(seed)
    sdk_version = importlib.metadata.version("mcp")
    if sdk_version != "2.2.0":
        raise RuntimeError(f"MCP 2.2.0 is required, found {sdk_version}")
    factory = episode_factory or harbor_mirrors_episode_factory(root, image=image)
    mcp_server = _mcp_server(factory, seed)
    app = mcp_server.streamable_http_app(json_response=True, stateless_http=True)
    server = start_loopback_server(app)
    started = time.monotonic()
    try:
        transport = asyncio.run(_drive_live_harbor(server.port, seed))
    finally:
        server.close()

    physical = _physical_result(root, transport)
    evaluation = summarize_mirrors_episode(seed, physical)
    operation = transport["operation"]
    passed = operation["lifecycle"] == "succeeded" and evaluation["outcome"] == "passed"

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"mcp-harbor-s{seed}-{stamp}-{uuid.uuid4().hex[:8]}"
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
            "adapter": "entryplug_mcp",
            "sdk": "mcp",
            "sdk_version": sdk_version,
            "protocol": "streamable_http",
            "protocol_revision": MCP_PROTOCOL_REVISION,
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
            "capability through Streamable HTTP MCP. It verifies the same physical launch and "
            "acquisition path; individual physical probes are not separate MCP tool calls."
        ),
    }
    with (evidence_path / "mcp.json").open("x", encoding="utf-8") as stream:
        json.dump(record, stream, allow_nan=False, indent=2, sort_keys=True)
        stream.write("\n")
    return McpHarborResult(
        run_id,
        evidence_path,
        physical.run_id,
        physical.evidence_path,
        passed,
    )
