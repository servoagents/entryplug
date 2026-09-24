"""Replay real Hall of Mirrors evidence through the OpenEnv transport."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import json
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from entryplug.association import CandidateEvidence
from entryplug.association_operation import CAPABILITY_NAME, association_host, candidate_records
from entryplug.episode import EpisodeStart
from entryplug.mirror_replay import load_mirror_replay_source
from entryplug.qualification import HARBOR_MIRRORS_V1
from entryplug.session import Session
from entryplug_openenv.client import EntryplugEnvClient
from entryplug_openenv.environment import EntryplugEnvironment
from entryplug_openenv.loopback import LoopbackServer, start_loopback_server
from entryplug_openenv.models import (
    ActDecision,
    EntryplugAction,
    InspectDecision,
    StopDecision,
    WaitDecision,
)
from entryplug_openenv.server import create_entryplug_app


@dataclass(frozen=True, slots=True)
class OpenEnvMirrorReplayResult:
    """Outcome and create-only evidence location for one replay."""

    run_id: str
    evidence_path: Path
    passed: bool


def _input_digest(candidates: Sequence[CandidateEvidence]) -> str:
    encoded = json.dumps(
        candidate_records(candidates),
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _start_server(source_run_id: str) -> LoopbackServer:
    async def factory(seed: int) -> EpisodeStart:
        host = association_host(
            HARBOR_MIRRORS_V1.association,
            runtime_id=f"openenv-mirror-replay-{seed}-{uuid.uuid4().hex}",
        )
        return EpisodeStart(
            Session(host, owns_runtime=True),
            {
                "fixture": "recorded_hall_of_mirrors_association",
                "physics_rerun": False,
                "qualification_profile": HARBOR_MIRRORS_V1.profile_id,
                "qualification_profile_digest": HARBOR_MIRRORS_V1.digest,
                "source_run_id": source_run_id,
            },
        )

    app = create_entryplug_app(
        lambda: EntryplugEnvironment(
            factory,
            episode_timeout_seconds=10.0,
            observation_window_seconds=2.0,
        )
    )
    return start_loopback_server(
        app,
        thread_name="entryplug-openenv-replay",
    )


async def _replay(
    seed: int,
    candidates: Sequence[CandidateEvidence],
    port: int,
) -> dict[str, Any]:
    digest = _input_digest(candidates)
    request_id = f"mirror-replay-{digest}"
    async with EntryplugEnvClient(base_url=f"http://127.0.0.1:{port}") as client:
        initial = await client.reset(seed=seed)
        accepted = await client.step(
            EntryplugAction(
                decision=ActDecision(
                    capability=CAPABILITY_NAME,
                    arguments={"candidates": candidate_records(candidates)},
                    request_id=request_id,
                )
            )
        )
        accepted_result = accepted.observation.result or {}
        operation_id = accepted_result.get("operation_id")
        if not isinstance(operation_id, str) or not operation_id:
            raise RuntimeError("OpenEnv did not return an association operation ID")
        if accepted_result.get("request_id") != request_id:
            raise RuntimeError("OpenEnv did not preserve the stable association request ID")
        completed = await client.step(
            EntryplugAction(
                decision=WaitDecision(operation_id=operation_id, timeout_seconds=1.0)
            )
        )
        completed_result = completed.observation.result or {}
        if completed_result.get("lifecycle") != "succeeded":
            raise RuntimeError("OpenEnv association operation did not succeed")
        inspected = await client.step(
            EntryplugAction(
                decision=InspectDecision(reference=operation_id, detail="result")
            )
        )
        await client.step(
            EntryplugAction(decision=StopDecision(reason="mirror replay complete"))
        )
        state = await client.state()

    snapshot = inspected.observation.result or {}
    selection = snapshot.get("result")
    if not isinstance(selection, dict):
        raise RuntimeError("OpenEnv inspection returned no association result")
    return {
        "initial": initial.observation.state.model_dump(mode="json"),
        "final_state": state.model_dump(mode="json"),
        "operation": {
            "operation_id": operation_id,
            "lifecycle": snapshot.get("lifecycle"),
            "motion_state": snapshot.get("motion_state"),
            "request_id": request_id,
        },
        "selection": selection,
    }


def run_openenv_mirror_replay(root: Path, source_run_id: str) -> OpenEnvMirrorReplayResult:
    """Use the real SDK transport, then score the result against withheld roles."""

    sdk_version = importlib.metadata.version("openenv")
    if sdk_version != "0.5.0":
        raise RuntimeError(f"OpenEnv 0.5.0 is required, found {sdk_version}")
    source = load_mirror_replay_source(root, source_run_id)
    started = time.monotonic()
    server = _start_server(source.run_id)
    try:
        transport = asyncio.run(_replay(source.seed, source.candidates, server.port))
    finally:
        server.close()

    selection = transport["selection"]
    replay_candidate = selection.get("candidate_id")
    if not isinstance(replay_candidate, str):
        raise RuntimeError("OpenEnv association did not select a candidate")
    selected_role = source.private_source_roles.get(replay_candidate)
    equivalent = (
        replay_candidate == source.recorded_candidate_id == source.direct_candidate_id
    )
    controlled = selected_role == "controlled_rendered_camera"
    passed = equivalent and controlled

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"openenv-mirrors-replay-{stamp}-{uuid.uuid4().hex[:8]}"
    evidence_path = root / "runs" / run_id
    evidence_path.mkdir(parents=True, exist_ok=False)
    record = {
        "schema_version": 1,
        "recorded_at": datetime.now(UTC).isoformat(),
        "status": "passed" if passed else "failed",
        "source": {
            "run_id": source.run_id,
            "experiment_seed": source.seed,
            "candidate_count": len(source.candidates),
            "candidate_input_digest": f"sha256:{_input_digest(source.candidates)}",
        },
        "qualification_profile": {
            "profile_id": HARBOR_MIRRORS_V1.profile_id,
            "digest": HARBOR_MIRRORS_V1.digest,
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
        "comparison": {
            "recorded_candidate_id": source.recorded_candidate_id,
            "direct_candidate_id": source.direct_candidate_id,
            "openenv_candidate_id": replay_candidate,
            "equivalent_selection": equivalent,
        },
        "private_evaluation": {
            "selected_role": selected_role,
            "controlled_source_selected": controlled,
            "roles_exposed_to_environment": False,
        },
        "claim_boundary": (
            "This replay sends recorded opaque sensor evidence through the OpenEnv SDK and "
            "Entryplug operation host. It verifies protocol and selection equivalence; it does "
            "not rerun Harbor physics or establish equivalent physical outcomes."
        ),
    }
    with (evidence_path / "replay.json").open("x", encoding="utf-8") as stream:
        json.dump(record, stream, allow_nan=False, indent=2, sort_keys=True)
        stream.write("\n")
    return OpenEnvMirrorReplayResult(run_id, evidence_path, passed)
