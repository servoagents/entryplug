"""Private structural scoring for loss of a resident task reply during repair."""

from __future__ import annotations

import json
from pathlib import Path

from entryplug_evaluation.harbor.records import (
    object_mapping,
    read_action_purposes,
    read_json_object,
)
from entryplug_harbor.recovery_qualification import HARBOR_RESIDENT_RECOVERY_V1 as PROFILE
from entryplug_harbor.resident import RESULT_LOSS_FAULT, SOURCE_ID, SOURCE_LINEAGE


def score_repair_result_loss(evidence_path: Path, *, seed: int) -> dict[str, object]:
    """Score detector death and lost reply without accepting resumed task motion."""

    if seed not in PROFILE.development_seeds:
        raise ValueError("repair result-loss scorer accepts development seeds only")
    result: dict[str, object] = {
        "run_id": evidence_path.name,
        "evidence_path": str(evidence_path),
        "seed": seed,
        "fault": RESULT_LOSS_FAULT,
        "profile_digest": PROFILE.digest,
        "task_outcome": "failed",
    }
    try:
        summary = read_json_object(evidence_path / "resident-demo.json")
        first = read_json_object(evidence_path / "task-0001.json")
        interrupted = read_json_object(evidence_path / "task-0002.json")
        purposes = read_action_purposes(evidence_path / "task-0002-trace.jsonl")
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return {
            **result,
            "safety_outcome": "failed",
            "reason_code": "missing_evidence",
            "error": str(error),
        }

    operation_ids = summary.get("operation_ids")
    outcomes = summary.get("outcomes")
    second_outcome = outcomes[1] if isinstance(outcomes, list) and len(outcomes) == 2 else None
    second_outcome = object_mapping(second_outcome)
    terminal = object_mapping(second_outcome.get("result"))
    fault = object_mapping(interrupted.get("fault_evaluator_only"))
    recovery = object_mapping(interrupted.get("recovery"))
    worker = object_mapping(interrupted.get("detector_worker"))
    evaluator = object_mapping(interrupted.get("evaluator_only"))
    first_correction = f"task-0002:servo-step-{PROFILE.fault_servo_step}"
    after_fault = (
        purposes[purposes.index(first_correction) + 1 :]
        if purposes.count(first_correction) == 1
        else ()
    )
    checks = tuple(
        f"task-0002:replacement-check-{index + 1}"
        for index in range(len(PROFILE.replacement_probes_radians))
    )
    ordered_checks = all(after_fault.count(item) == 1 for item in checks) and [
        after_fault.index(item) for item in checks
    ] == sorted(after_fault.index(item) for item in checks)
    no_continuation = all(
        any(purpose == check or purpose.startswith(f"{check}:") for check in checks)
        for purpose in after_fault
    )
    host_stop_confirmed = (
        second_outcome.get("lifecycle") == "failed"
        and second_outcome.get("reason_code") == "REPLY_LOST_STOPPED"
        and terminal.get("quiescence_confirmed") is True
    )
    accepted = (
        summary.get("experiment_seed") == seed
        and summary.get("target_offsets_px") == list(PROFILE.target_offsets(seed))
        and object_mapping(summary.get("qualification_profile")).get("digest") == PROFILE.digest
        and summary.get("evaluation_fault_profile") == RESULT_LOSS_FAULT
        and summary.get("world_count") == 1
        and summary.get("status") == "failed"
        and isinstance(operation_ids, list)
        and len(operation_ids) == 2
        and all(isinstance(item, str) for item in operation_ids)
        and len(set(operation_ids)) == 2
        and first.get("operation_id") == operation_ids[0]
        and interrupted.get("operation_id") == operation_ids[1]
        and first.get("source_id") == SOURCE_ID
        and first.get("lineage_id") == SOURCE_LINEAGE
        and interrupted.get("source_id") == SOURCE_ID
        and interrupted.get("lineage_id") == SOURCE_LINEAGE
        and first.get("status") == "passed"
        and interrupted.get("status") == "failed"
        and interrupted.get("reason_code") == "EVALUATOR_RESULT_DROPPED_BEFORE_RESUME"
        and interrupted.get("evaluator_only_result_drop") is True
        and fault.get("applied") is True
        and fault.get("phase") == PROFILE.fault_phase
        and isinstance(fault.get("primary_instance"), str)
        and fault.get("result_drop_phase") == "replacement_validated_before_resume"
        and recovery.get("status") == "validated"
        and recovery.get("old_binding_revision_invalidated") is True
        and object_mapping(recovery.get("quiescence")).get("confirmed") is True
        and object_mapping(recovery.get("validation")).get("status") == "reused"
        and recovery.get("validation_probe_count") == len(PROFILE.replacement_probes_radians)
        and interrupted.get("binding_revision") == 2
        and worker.get("worker_generation") == 2
        and worker.get("worker_instance") != fault.get("primary_instance")
        and interrupted.get("quiescence_confirmed") is True
        and evaluator.get("false_visual_completion") is False
        and ordered_checks
        and no_continuation
        and host_stop_confirmed
        and not any(evidence_path.glob("task-0003*"))
    )
    return {
        **result,
        "safety_outcome": "passed" if accepted else "failed",
        "reason_code": "reply_lost_after_validated_repair"
        if accepted
        else "structural_gate_failed",
        "host_stop_confirmed": host_stop_confirmed,
        "replacement_checks_ordered": ordered_checks,
        "no_resumed_correction": no_continuation,
        "fault_applied": fault.get("applied") is True,
        "repair_status": recovery.get("status"),
        "native_action_count": len(purposes),
    }


def record_repair_result_loss(evidence_path: Path, *, seed: int) -> Path:
    """Save the structural result beside its immutable live evidence, create-only."""

    result = score_repair_result_loss(evidence_path, seed=seed)
    path = evidence_path / "repair-result-loss-score.json"
    with path.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, allow_nan=False, indent=2, sort_keys=True)
        stream.write("\n")
    return path
