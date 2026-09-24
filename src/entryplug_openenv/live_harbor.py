"""Drive one live owned Harbor acquisition through the OpenEnv SDK."""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from entryplug.episode import EpisodeFactory
from entryplug.harbor_episode import ACQUIRE_VISUAL_BINDING, harbor_mirrors_episode_factory
from entryplug.pilot import summarize_mirrors_episode
from entryplug.qualification import HARBOR_MIRRORS_V1
from entryplug.runtime import DEFAULT_HARBOR_IMAGE, RuntimeResult, RUN_ID_PATTERN
from entryplug.seeding import validate_experiment_seed
from entryplug_openenv.client import EntryplugEnvClient
from entryplug_openenv.environment import EntryplugEnvironment
from entryplug_openenv.loopback import start_loopback_server
from entryplug_openenv.models import (
    ActDecision,
    EntryplugAction,
    InspectDecision,
    StopDecision,
    WaitDecision,
)
from entryplug_openenv.server import create_entryplug_app

TERMINAL_LIFECYCLES = {
    "succeeded",
    "failed",
    "canceled",
    "rejected",
    "indeterminate",
}


@dataclass(frozen=True, slots=True)
class OpenEnvHarborResult:
    """Outcome and create-only wrapper evidence for one live SDK episode."""

    run_id: str
    evidence_path: Path
    physical_run_id: str
    physical_evidence_path: Path
    passed: bool


async def _drive_live_harbor(port: int, seed: int) -> dict[str, Any]:
    request_id = f"openenv-harbor-{seed}-{HARBOR_MIRRORS_V1.digest.removeprefix('sha256:')}"
    async with EntryplugEnvClient(base_url=f"http://127.0.0.1:{port}") as client:
        initial = await client.reset(seed=seed)
        accepted = await client.step(
            EntryplugAction(
                decision=ActDecision(
                    capability=ACQUIRE_VISUAL_BINDING,
                    arguments={},
                    request_id=request_id,
                )
            )
        )
        accepted_result = accepted.observation.result or {}
        operation_id = accepted_result.get("operation_id")
        if not isinstance(operation_id, str) or not operation_id:
            raise RuntimeError("OpenEnv did not return a Harbor operation ID")
        if accepted_result.get("request_id") != request_id:
            raise RuntimeError("OpenEnv did not preserve the stable Harbor request ID")

        wait_count = 0
        current: dict[str, Any] = accepted_result
        while current.get("lifecycle") not in TERMINAL_LIFECYCLES:
            waited = await client.step(
                EntryplugAction(
                    decision=WaitDecision(
                        operation_id=operation_id,
                        timeout_seconds=5.0,
                    )
                )
            )
            wait_count += 1
            current = waited.observation.result or {}
            if wait_count >= 40 and current.get("lifecycle") not in TERMINAL_LIFECYCLES:
                raise RuntimeError("OpenEnv Harbor operation exceeded forty bounded waits")

        inspected = await client.step(
            EntryplugAction(
                decision=InspectDecision(reference=operation_id, detail="result")
            )
        )
        stopped = await client.step(
            EntryplugAction(decision=StopDecision(reason="live Harbor episode complete"))
        )
        state = await client.state()

    snapshot = inspected.observation.result or {}
    public_result = snapshot.get("result")
    if not isinstance(public_result, dict):
        raise RuntimeError("OpenEnv inspection returned no Harbor operation result")
    return {
        "initial_state": initial.observation.state.model_dump(mode="json"),
        "final_state": state.model_dump(mode="json"),
        "done": stopped.done,
        "sdk_wait_count": wait_count,
        "model_decision_count": 0,
        "operation": {
            "operation_id": operation_id,
            "request_id": request_id,
            "lifecycle": snapshot.get("lifecycle"),
            "motion_state": snapshot.get("motion_state"),
            "reason_code": snapshot.get("reason_code"),
            "result": public_result,
        },
    }


def _physical_result(root: Path, transport: Mapping[str, Any]) -> RuntimeResult:
    operation = transport.get("operation")
    result = operation.get("result") if isinstance(operation, Mapping) else None
    if not isinstance(result, Mapping):
        raise ValueError("OpenEnv Harbor result is not an object")
    run_id = result.get("run_id")
    raw_path = result.get("evidence_path")
    if not isinstance(run_id, str) or RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise ValueError("OpenEnv Harbor result has no valid physical run ID")
    if not isinstance(raw_path, str):
        raise ValueError("OpenEnv Harbor result has no physical evidence path")
    runs = (root / "runs").resolve()
    evidence_path = Path(raw_path).resolve()
    expected = (runs / run_id).resolve()
    if evidence_path != expected or not evidence_path.is_relative_to(runs):
        raise ValueError("OpenEnv Harbor evidence resolves outside its declared run")
    return RuntimeResult(0, run_id, evidence_path)


def run_openenv_harbor(
    root: Path,
    *,
    seed: int,
    image: str = DEFAULT_HARBOR_IMAGE,
    episode_factory: EpisodeFactory | None = None,
) -> OpenEnvHarborResult:
    """Run one complete live task through SDK transport and private evaluation."""

    validate_experiment_seed(seed)
    sdk_version = importlib.metadata.version("openenv")
    if sdk_version != "0.5.0":
        raise RuntimeError(f"OpenEnv 0.5.0 is required, found {sdk_version}")
    factory = episode_factory or harbor_mirrors_episode_factory(root, image=image)
    app = create_entryplug_app(
        lambda: EntryplugEnvironment(
            factory,
            episode_timeout_seconds=210.0,
            observation_window_seconds=5.0,
        )
    )
    server = start_loopback_server(app, thread_name="entryplug-openenv-live-harbor")
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
    run_id = f"openenv-harbor-s{seed}-{stamp}-{uuid.uuid4().hex[:8]}"
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
            "adapter": "entryplug_openenv",
            "sdk": "openenv",
            "sdk_version": sdk_version,
            "protocol": "websocket",
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
            "roles_exposed_to_environment": False,
        },
        "claim_boundary": (
            "This run executes the existing Harbor Hall of Mirrors task as one task level "
            "capability through OpenEnv. It verifies the same physical launch and acquisition "
            "path; individual physical probes are not separate OpenEnv steps."
        ),
    }
    with (evidence_path / "online.json").open("x", encoding="utf-8") as stream:
        json.dump(record, stream, allow_nan=False, indent=2, sort_keys=True)
        stream.write("\n")
    return OpenEnvHarborResult(
        run_id,
        evidence_path,
        physical.run_id,
        physical.evidence_path,
        passed,
    )
