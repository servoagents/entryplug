"""Private direct-client baseline for the resident visual recovery experiment."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from entryplug.evidence import JsonValue, json_object
from entryplug.harbor_episode import ImageCheck, docker_image_available
from entryplug.harbor_resident import (
    SOURCE_ID,
    SOURCE_LINEAGE,
    ResidentReplyLost,
    StartResident,
    start_owned_resident,
)
from entryplug.recovery_qualification import HARBOR_RESIDENT_RECOVERY_V1 as PROFILE
from entryplug.runtime import DEFAULT_HARBOR_IMAGE, new_harbor_run_id


@dataclass(frozen=True, slots=True)
class HandwrittenResult:
    passed: bool
    run_id: str
    evidence_path: Path
    operation_ids: tuple[str, ...]


def _task_score(path: Path) -> Mapping[str, object]:
    try:
        task = json.loads(path.read_text(encoding="utf-8"))
        score = task["evaluator_only"]
        if isinstance(score, dict):
            return score
    except (OSError, KeyError, ValueError, json.JSONDecodeError):
        pass
    return {"status": "missing_or_invalid"}


async def run_handwritten_recovery(
    root: Path,
    *,
    seed: int = 101,
    image: str = DEFAULT_HARBOR_IMAGE,
    evaluation_fault: str | None = None,
    start_resident: StartResident = start_owned_resident,
    image_check: ImageCheck = docker_image_available,
) -> HandwrittenResult:
    """Run the same physical task without an agent child or operation host.

    This is a controlled performance baseline, not a general client API: it lacks
    host admission, mutation deduplication, and the authoritative operation ledger.
    """

    if evaluation_fault not in {None, "kill-active-worker"}:
        raise ValueError("unsupported handwritten recovery fault")
    offsets = PROFILE.target_offsets(seed)
    if not await image_check(image):
        raise FileNotFoundError(f"container image {image!r} is unavailable")

    run_id = new_harbor_run_id("resident")
    started = time.monotonic()
    run = await start_resident(root, image, run_id)
    startup_ms = round((time.monotonic() - started) * 1000, 3)
    ready = run.ready
    initial_value = ready.get("initial_y_px")
    if (
        ready.get("source_id") != SOURCE_ID
        or ready.get("lineage_id") != SOURCE_LINEAGE
        or isinstance(initial_value, bool)
        or not isinstance(initial_value, (int, float))
    ):
        await run.stop()
        raise ValueError("resident ready record has no supported visual source")
    initial = float(initial_value)
    targets = tuple(round(initial + offset, 6) for offset in offsets)
    if any(not 0 <= target < 480 for target in targets):
        await run.stop()
        raise ValueError("fixture did not provide four image-space targets")
    acquisition_value = ready.get("acquisition_ms")
    container_startup_ms = (
        round(max(0.0, startup_ms - float(acquisition_value)), 3)
        if isinstance(acquisition_value, (int, float))
        and not isinstance(acquisition_value, bool)
        and 0 <= acquisition_value <= startup_ms
        else None
    )

    operation_ids: list[str] = []
    outcomes: list[Mapping[str, JsonValue]] = []
    evaluator_results: list[Mapping[str, object]] = []
    failure_reason: str | None = None
    episode_wall_ms = 0.0
    stopped_confirmed = False
    try:
        for index, target in enumerate(targets, start=1):
            operation_id = f"handwritten-{uuid.uuid4().hex}"
            deadline = time.monotonic() + PROFILE.operation_deadline_seconds
            operation_ids.append(operation_id)
            goal = {
                "schema_version": 1,
                "operation_id": operation_id,
                "runtime_id": run_id,
                "run_id": run_id,
                "source_id": SOURCE_ID,
                "lineage_id": SOURCE_LINEAGE,
                "target_y_px": target,
                "deadline_monotonic": deadline,
            }
            run.evidence_path.mkdir(parents=True, exist_ok=True)
            with (run.evidence_path / f"goal-{operation_id}.json").open(
                "x", encoding="utf-8"
            ) as stream:
                json.dump(goal, stream, allow_nan=False, sort_keys=True)
                stream.write("\n")
            fault = evaluation_fault if index == PROFILE.fault_task_index else None
            try:
                reply = await run.request(
                    operation_id,
                    target,
                    evaluation_fault=fault,
                    deadline_monotonic=deadline,
                )
            except (ResidentReplyLost, OSError, TimeoutError, ValueError) as error:
                failure_reason = f"reply_lost:{type(error).__name__}"
                break
            outcomes.append(reply)
            evaluator_results.append(_task_score(run.evidence_path / f"task-{index:04d}.json"))
            timely = time.monotonic() <= deadline
            if (
                reply.get("operation_id") != operation_id
                or reply.get("source_id") != SOURCE_ID
                or reply.get("lineage_id") != SOURCE_LINEAGE
                or reply.get("status") != "passed"
                or reply.get("binding_ready") is not True
                or reply.get("visual_capability_available") is not True
                or not timely
                or reply.get("quiescence_confirmed") is not True
                or evaluator_results[-1].get("independently_inside_tolerance") is not True
                or evaluator_results[-1].get("false_visual_completion") is not False
            ):
                if not timely:
                    failure_reason = "DEADLINE_EXCEEDED"
                elif reply.get("status") != "passed":
                    failure_reason = str(reply.get("reason_code", "TASK_NOT_VERIFIED"))
                else:
                    failure_reason = "TASK_NOT_VERIFIED"
                break
        episode_wall_ms = round((time.monotonic() - started) * 1000, 3)
    finally:
        stopped_confirmed = (await run.stop()).confirmed

    passed = len(outcomes) == len(targets) and failure_reason is None and stopped_confirmed
    payload = json_object(
        {
            "schema_version": 1,
            "status": "passed" if passed else "failed",
            "execution_path": "direct_handwritten",
            "run_id": run_id,
            "experiment_seed": seed,
            "target_offsets_px": offsets,
            "qualification_profile": {
                "profile_id": PROFILE.profile_id,
                "digest": PROFILE.digest,
            },
            "seed_scope": "image-row target offsets only; world physics and fault phase are fixed",
            "runtime_id": run_id,
            "source_id": SOURCE_ID,
            "lineage_id": SOURCE_LINEAGE,
            "initial_y_px": initial,
            "targets_y_px": targets,
            "operation_ids": operation_ids,
            "request_ids": [],
            "outcomes": outcomes,
            "world_count": 1,
            "evaluation_fault_profile": evaluation_fault,
            "evaluation_recovery_strategy": "checked_reuse",
            "private_evaluator": {
                "all_targets_inside_tolerance": all(
                    item.get("independently_inside_tolerance") is True for item in evaluator_results
                )
                and len(evaluator_results) == len(targets),
                "results": evaluator_results,
            },
            "container_startup_and_acquisition_ms": startup_ms,
            "container_startup_ms": container_startup_ms,
            "acquisition_ms": acquisition_value,
            "episode_wall_ms": episode_wall_ms,
            "agent_startup_ms": None,
            "agent_process_ids": [],
            "agent_decision_count": 0,
            "handwritten_task_decision_count": len(operation_ids),
            "model_decision_count": 0,
            "world_stop_confirmed": stopped_confirmed,
            "failure_reason": failure_reason,
            "claim_boundary": (
                "Direct evaluator client shares Harbor numerical control and recovery code. "
                "It bypasses host admission, deduplication, and the agent child."
            ),
        },
        "resident handwritten baseline",
    )
    with (run.evidence_path / "resident-demo.json").open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2, sort_keys=True)
        stream.write("\n")
    return HandwrittenResult(passed, run_id, run.evidence_path, tuple(operation_ids))
