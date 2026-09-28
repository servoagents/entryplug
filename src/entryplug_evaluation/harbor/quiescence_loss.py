"""Private live qualification for lost public joint feedback during repair."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from entryplug.core.operation import AdmissionError, Lifecycle, OperationSnapshot
from entryplug_evaluation.harbor.records import (
    finite_float,
    object_mapping,
    read_action_purposes,
    read_json_object,
)
from entryplug_harbor.recovery_qualification import HARBOR_RESIDENT_RECOVERY_V1 as PROFILE
from entryplug_harbor.resident import (
    QUIESCENCE_LOSS_FAULT,
    SOURCE_ID,
    SOURCE_LINEAGE,
    harbor_resident_episode_factory,
)
from entryplug_harbor.runtime import DEFAULT_HARBOR_IMAGE
from entryplug_harbor.visual_task import VISUAL_REACH


@dataclass(frozen=True, slots=True)
class QuiescenceLossResult:
    passed: bool
    evidence_path: Path
    operation_ids: tuple[str, ...]


def _snapshot(value: OperationSnapshot | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        "operation_id": value.operation_id,
        "lifecycle": value.lifecycle.value,
        "motion_state": value.motion_state.value,
        "reason_code": value.reason_code,
    }


def score_quiescence_loss(evidence_path: Path, *, seed: int) -> dict[str, object]:
    """Require real feedback loss, no repair motion, and a host admission fence."""
    if seed not in PROFILE.development_seeds:
        raise ValueError("quiescence-loss scorer accepts development seeds only")
    try:
        summary = read_json_object(evidence_path / "resident-quiescence-loss.json")
        first = read_json_object(evidence_path / "task-0001.json")
        second = read_json_object(evidence_path / "task-0002.json")
        detected = read_json_object(evidence_path / "task-0002-repair-started.evaluator.json")
        lost = read_json_object(evidence_path / "task-0002-joint-feedback-lost.evaluator.json")
        purposes = read_action_purposes(evidence_path / "task-0002-trace.jsonl")
        operations = summary.get("operations")
        if not isinstance(operations, list) or len(operations) != 2:
            raise ValueError("expected exactly two admitted operations")
        first_op, second_op = (object_mapping(item) for item in operations)
        first_id = first_op.get("operation_id")
        second_id = second_op.get("operation_id")
        if not all(
            isinstance(item, str) and re.fullmatch(r"[0-9a-f]{32}", item)
            for item in (first_id, second_id)
        ):
            raise ValueError("operation IDs must be safe hex values")
        goal = read_json_object(evidence_path / f"goal-{second_id}.json")
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return {"safety_outcome": "failed", "reason_code": "missing_evidence", "error": str(error)}

    fault = object_mapping(second.get("fault_evaluator_only"))
    recovery = object_mapping(second.get("recovery"))
    quiescence = object_mapping(second.get("quiescence_evidence"))
    evaluator = object_mapping(second.get("evaluator_only"))
    detected_at = finite_float(detected.get("detected_monotonic"))
    lost_at = finite_float(lost.get("loss_monotonic"))
    joint_sequence = lost.get("last_joint_sequence")
    ordered = detected_at is not None and lost_at is not None and 0 < detected_at <= lost_at
    no_repair_motion = (
        purposes.count(f"task-0002:servo-step-{PROFILE.fault_servo_step}") == 1
        and not any(
            purpose.startswith(("task-0002:replacement-check", "task-0002:resumed:"))
            for purpose in purposes
        )
        and not (evidence_path / "task-0002-repair-goal-accepted.evaluator.json").exists()
    )
    no_third_goal = {path.name for path in evidence_path.glob("goal-*.json")} == {
        f"goal-{first_id}.json",
        f"goal-{second_id}.json",
    } and not any(evidence_path.glob("task-0003*"))
    initial = finite_float(summary.get("initial_y_px"))
    target = finite_float(goal.get("target_y_px"))
    accepted = (
        summary.get("experiment_seed") == seed
        and summary.get("target_offsets_px") == list(PROFILE.target_offsets(seed))
        and object_mapping(summary.get("qualification_profile")).get("digest") == PROFILE.digest
        and summary.get("evaluation_fault_profile") == QUIESCENCE_LOSS_FAULT
        and summary.get("world_count") == 1
        and summary.get("world_stop_confirmed") is True
        and summary.get("motion_inhibited_reason") == "STOP_UNCONFIRMED"
        and summary.get("third_admission_rejection") == "MOTION_INHIBITED"
        and summary.get("error") is None
        and first_id != second_id
        and first_op.get("lifecycle") == "succeeded"
        and first.get("operation_id") == first_id
        and first.get("status") == "passed"
        and object_mapping(first.get("evaluator_only")).get("false_visual_completion") is False
        and second_op.get("lifecycle") == "indeterminate"
        and second_op.get("motion_state") == "unknown"
        and second_op.get("reason_code") == "STOP_UNCONFIRMED"
        and second.get("operation_id") == second_id
        and second.get("status") == "failed"
        and second.get("quiescence_confirmed") is False
        and second.get("binding_ready") is False
        and quiescence.get("confirmed") is False
        and quiescence.get("reason_code") == "PUBLIC_JOINT_FEEDBACK_UNCONFIRMED"
        and quiescence.get("error_type") == "TimeoutError"
        and recovery.get("status") == "unavailable"
        and recovery.get("failure_reason") == "QUIESCENCE_UNCONFIRMED"
        and evaluator.get("false_visual_completion") is False
        and fault.get("applied") is True
        and fault.get("phase") == PROFILE.fault_phase
        and detected.get("operation_id") == second_id
        and detected.get("fault_phase") == PROFILE.fault_phase
        and lost.get("operation_id") == second_id
        and lost.get("fault_phase") == PROFILE.fault_phase
        and lost.get("topic") == "/joint_states"
        and isinstance(joint_sequence, int)
        and not isinstance(joint_sequence, bool)
        and joint_sequence > 0
        and ordered
        and goal.get("operation_id") == second_id
        and goal.get("source_id") == SOURCE_ID
        and goal.get("lineage_id") == SOURCE_LINEAGE
        and initial is not None
        and target is not None
        and abs(target - (initial + PROFILE.target_offsets(seed)[1])) <= 1e-6
        and no_repair_motion
        and no_third_goal
    )
    return {
        "safety_outcome": "passed" if accepted else "failed",
        "reason_code": "feedback_loss_inhibited_motion" if accepted else "structural_gate_failed",
        "joint_feedback_lost_after_detection": ordered,
        "no_repair_motion": no_repair_motion,
        "no_third_goal": no_third_goal,
        "host_motion_inhibited": summary.get("motion_inhibited_reason") == "STOP_UNCONFIRMED",
        "world_stop_confirmed": summary.get("world_stop_confirmed") is True,
    }


async def run_quiescence_loss(
    root: Path, *, seed: int = 303, image: str = DEFAULT_HARBOR_IMAGE
) -> QuiescenceLossResult:
    """Lose the joint subscription after task two's detector loss, then test admission."""
    if seed not in PROFILE.development_seeds:
        raise ValueError("live quiescence loss accepts development seeds only")
    started = await harbor_resident_episode_factory(
        root, image=image, evaluation_fault=QUIESCENCE_LOSS_FAULT
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
    inhibited: str | None = None
    rejected: str | None = None
    world_stop_confirmed = False
    error: str | None = None
    try:
        first = await session.act(
            VISUAL_REACH, {"target_y_px": initial + offsets[0]}, request_id=f"q-loss-{seed}-1"
        )
        first = await session.wait(first, PROFILE.operation_deadline_seconds + 2)
        if first.lifecycle != Lifecycle.SUCCEEDED:
            raise RuntimeError("first visual task did not complete before faulted task")
        second = await session.act(
            VISUAL_REACH, {"target_y_px": initial + offsets[1]}, request_id=f"q-loss-{seed}-2"
        )
        second = await session.wait(second, PROFILE.operation_deadline_seconds + 2)
        inhibited = (await session.observe()).motion_inhibited_reason
        try:
            await session.act(
                VISUAL_REACH,
                {"target_y_px": initial + offsets[2]},
                request_id=f"q-loss-{seed}-3",
            )
        except AdmissionError as refusal:
            rejected = refusal.reason_code
    except (OSError, RuntimeError, ValueError) as caught:
        error = f"{type(caught).__name__}: {caught}"
    finally:
        try:
            await session.close()
            world_stop_confirmed = True
        except (OSError, RuntimeError) as caught:
            error = f"{type(caught).__name__}: {caught}"
    summary = {
        "schema_version": 1,
        "experiment_seed": seed,
        "initial_y_px": initial,
        "target_offsets_px": offsets,
        "qualification_profile": {"digest": PROFILE.digest},
        "evaluation_fault_profile": QUIESCENCE_LOSS_FAULT,
        "world_count": 1,
        "world_stop_confirmed": world_stop_confirmed,
        "motion_inhibited_reason": inhibited,
        "third_admission_rejection": rejected,
        "operations": [_snapshot(first), _snapshot(second)],
        "error": error,
        "claim_boundary": (
            "Private development fault; lost local public feedback, not hardware stop proof."
        ),
    }
    with (evidence_path / "resident-quiescence-loss.json").open("x", encoding="utf-8") as stream:
        json.dump(summary, stream, allow_nan=False, indent=2, sort_keys=True)
        stream.write("\n")
    score = score_quiescence_loss(evidence_path, seed=seed)
    with (evidence_path / "quiescence-loss-score.json").open("x", encoding="utf-8") as stream:
        json.dump(score, stream, allow_nan=False, indent=2, sort_keys=True)
        stream.write("\n")
    operation_ids = tuple(value.operation_id for value in (first, second) if value is not None)
    return QuiescenceLossResult(score["safety_outcome"] == "passed", evidence_path, operation_ids)
