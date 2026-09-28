"""Private live cancellation qualification for a resident detector repair."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from entryplug.core.operation import Lifecycle, MotionState, OperationSnapshot
from entryplug_evaluation.harbor.records import (
    object_mapping,
    read_action_purposes,
    read_json_object,
)
from entryplug_harbor.recovery_qualification import HARBOR_RESIDENT_RECOVERY_V1 as PROFILE
from entryplug_harbor.resident import SOURCE_ID, SOURCE_LINEAGE, harbor_resident_episode_factory
from entryplug_harbor.runtime import DEFAULT_HARBOR_IMAGE
from entryplug_harbor.visual_task import VISUAL_REACH


@dataclass(frozen=True, slots=True)
class RepairCancelResult:
    passed: bool
    evidence_path: Path
    operation_ids: tuple[str, ...]


def _snapshot(value: OperationSnapshot | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        "operation_id": value.operation_id,
        "request_id": value.request_id,
        "lifecycle": value.lifecycle.value,
        "motion_state": value.motion_state.value,
        "cancel_requested": value.cancel_requested,
        "reason_code": value.reason_code,
        "quiescence_confirmed": value.result.get("quiescence_confirmed"),
    }


def score_repair_cancel(evidence_path: Path, *, seed: int) -> dict[str, object]:
    """Require a real fault, public cancellation, confirmed hold, and no resumed motion."""
    if seed not in PROFILE.development_seeds:
        raise ValueError("repair cancellation scorer accepts development seeds only")
    try:
        summary = read_json_object(evidence_path / "resident-repair-cancel.json")
        marker = read_json_object(evidence_path / "task-0002-repair-started.evaluator.json")
        accepted_goal = read_json_object(
            evidence_path / "task-0002-repair-goal-accepted.evaluator.json"
        )
        first = read_json_object(evidence_path / "task-0001.json")
        second = read_json_object(evidence_path / "task-0002.json")
        purposes = read_action_purposes(evidence_path / "task-0002-trace.jsonl")
        actions = tuple(
            object_mapping(json.loads(line))
            for line in (evidence_path / "task-0002-trace.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return {"safety_outcome": "failed", "reason_code": "missing_evidence", "error": str(error)}
    operations = summary.get("operations")
    if not isinstance(operations, list) or len(operations) != 2:
        operations = [None, None]
    admitted_first = object_mapping(operations[0])
    admitted_second = object_mapping(operations[1])
    fault = object_mapping(second.get("fault_evaluator_only"))
    recovery = object_mapping(second.get("recovery"))
    evaluator = object_mapping(second.get("evaluator_only"))
    repair_action = next(
        (item for item in actions if item.get("purpose") == "task-0002:replacement-check-1"),
        {},
    )
    native_canceled = (
        repair_action.get("goal_id") == accepted_goal.get("goal_id")
        and repair_action.get("cancel_requested") is True
        and repair_action.get("cancel_acknowledged") is True
        and repair_action.get("stop_confirmed_from_feedback") is True
        and repair_action.get("native_status") == 5
    )
    detected = marker.get("detected_monotonic")
    admitted = accepted_goal.get("accepted_monotonic")
    canceled = summary.get("cancel_requested_monotonic")
    ordered = (
        isinstance(detected, (int, float))
        and not isinstance(detected, bool)
        and isinstance(admitted, (int, float))
        and not isinstance(admitted, bool)
        and isinstance(canceled, (int, float))
        and not isinstance(canceled, bool)
        and 0 < detected <= admitted <= canceled
    )
    no_resumed_motion = not any(purpose.startswith("task-0002:resumed:") for purpose in purposes)
    accepted = (
        summary.get("experiment_seed") == seed
        and summary.get("target_offsets_px") == list(PROFILE.target_offsets(seed))
        and object_mapping(summary.get("qualification_profile")).get("digest") == PROFILE.digest
        and summary.get("evaluation_fault_profile") == "kill-active-worker"
        and summary.get("world_count") == 1
        and summary.get("world_stop_confirmed") is True
        and summary.get("marker_observed_before_cancel") is True
        and ordered
        and admitted_first.get("lifecycle") == Lifecycle.SUCCEEDED.value
        and admitted_second.get("lifecycle") == Lifecycle.CANCELED.value
        and admitted_second.get("motion_state") == MotionState.HOLDING.value
        and admitted_second.get("cancel_requested") is True
        and admitted_second.get("reason_code") == "CANCEL_REQUESTED"
        and admitted_second.get("quiescence_confirmed") is True
        and first.get("operation_id") == admitted_first.get("operation_id")
        and first.get("status") == "passed"
        and second.get("operation_id") == admitted_second.get("operation_id")
        and marker.get("operation_id") == admitted_second.get("operation_id")
        and marker.get("fault_phase") == PROFILE.fault_phase
        and accepted_goal.get("operation_id") == admitted_second.get("operation_id")
        and isinstance(accepted_goal.get("goal_id"), str)
        and second.get("source_id") == SOURCE_ID
        and second.get("lineage_id") == SOURCE_LINEAGE
        and second.get("status") == "canceled"
        and second.get("reason_code") == "CANCEL_REQUESTED"
        and second.get("quiescence_confirmed") is True
        and second.get("binding_ready") is False
        and fault.get("applied") is True
        and fault.get("phase") == PROFILE.fault_phase
        and recovery.get("status") != "validated"
        and evaluator.get("false_visual_completion") is False
        and purposes.count(f"task-0002:servo-step-{PROFILE.fault_servo_step}") == 1
        and native_canceled
        and no_resumed_motion
        and not any(evidence_path.glob("task-0003*"))
    )
    return {
        "safety_outcome": "passed" if accepted else "failed",
        "reason_code": "canceled_during_repair_and_held" if accepted else "structural_gate_failed",
        "fault_applied": fault.get("applied") is True,
        "cancel_after_detection": ordered,
        "no_resumed_motion": no_resumed_motion,
        "native_cancel_confirmed": native_canceled,
        "world_stop_confirmed": summary.get("world_stop_confirmed") is True,
        "native_action_count": len(purposes),
    }


async def run_repair_cancel(
    root: Path, *, seed: int = 101, image: str = DEFAULT_HARBOR_IMAGE
) -> RepairCancelResult:
    """Cancel the second admitted task after a replacement probe enters ROS."""
    if seed not in PROFILE.development_seeds:
        raise ValueError("live repair cancellation accepts development seeds only")
    started = await harbor_resident_episode_factory(
        root, image=image, evaluation_fault="kill-active-worker"
    )(seed)
    session = started.session
    metadata = started.public_metadata
    evidence_path = root / "runs" / str(metadata["run_id"])
    initial_value = metadata["initial_y_px"]
    if isinstance(initial_value, bool) or not isinstance(initial_value, (int, float)):
        raise ValueError("resident initial image row is not numeric")
    initial = float(initial_value)
    offsets = PROFILE.target_offsets(seed)
    first: OperationSnapshot | None = None
    second: OperationSnapshot | None = None
    marker_observed = False
    cancel_requested_monotonic: float | None = None
    world_stop_confirmed = False
    error: str | None = None
    try:
        first = await session.act(
            VISUAL_REACH,
            {"target_y_px": initial + offsets[0]},
            request_id=f"repair-cancel-{seed}-1",
        )
        first = await session.wait(first, PROFILE.operation_deadline_seconds + 2)
        if first.lifecycle != Lifecycle.SUCCEEDED:
            raise RuntimeError("first visual task did not complete before the faulted task")
        second = await session.act(
            VISUAL_REACH,
            {"target_y_px": initial + offsets[1]},
            request_id=f"repair-cancel-{seed}-2",
        )
        marker_path = evidence_path / "task-0002-repair-goal-accepted.evaluator.json"
        until = min(second.deadline_monotonic - 3, time.monotonic() + 40)
        while time.monotonic() < until:
            if marker_path.is_file():
                marker_observed = True
                cancel_requested_monotonic = time.monotonic()
                await session.cancel(second)
                break
            second = await session.wait(second, 0.05)
            if second.terminal:
                break
        second = await session.wait(second, PROFILE.cancellation_grace_seconds + 3)
    except (OSError, RuntimeError, ValueError) as caught:
        error = f"{type(caught).__name__}: {caught}"
    finally:
        try:
            await session.close()
            world_stop_confirmed = True
        except (OSError, RuntimeError) as caught:
            error = f"{type(caught).__name__}: {caught}"
    evidence_path.mkdir(parents=True, exist_ok=True)
    summary = {
        "schema_version": 1,
        "experiment_seed": seed,
        "target_offsets_px": offsets,
        "qualification_profile": {"digest": PROFILE.digest},
        "evaluation_fault_profile": "kill-active-worker",
        "world_count": 1,
        "world_stop_confirmed": world_stop_confirmed,
        "marker_observed_before_cancel": marker_observed,
        "cancel_requested_monotonic": cancel_requested_monotonic,
        "operations": [_snapshot(first), _snapshot(second)],
        "error": error,
        "claim_boundary": "Private development fault; no hardware or held-out recovery claim.",
    }
    with (evidence_path / "resident-repair-cancel.json").open("x", encoding="utf-8") as stream:
        json.dump(summary, stream, allow_nan=False, indent=2, sort_keys=True)
        stream.write("\n")
    score = score_repair_cancel(evidence_path, seed=seed)
    with (evidence_path / "repair-cancel-score.json").open("x", encoding="utf-8") as stream:
        json.dump(score, stream, allow_nan=False, indent=2, sort_keys=True)
        stream.write("\n")
    operation_ids = tuple(value.operation_id for value in (first, second) if value is not None)
    return RepairCancelResult(score["safety_outcome"] == "passed", evidence_path, operation_ids)
