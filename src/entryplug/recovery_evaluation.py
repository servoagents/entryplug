"""Reduce every offered resident task, including pre-fault failures."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from entryplug.association import load_visual_binding
from entryplug.evidence import EvidenceRecord
from entryplug.recovery_qualification import HARBOR_RESIDENT_RECOVERY_V1 as PROFILE
from entryplug.recovery_records import (
    finite_float,
    nonnegative_int,
    object_mapping,
    read_action_purposes,
    read_json_object,
)

RECOVERABLE = "kill-active-worker"
INVALID_REPLACEMENTS = frozenset({"kill-both-workers", "kill-active-worker-stale-alternate"})


def _ordered_once(purposes: tuple[str, ...], required: tuple[str, ...]) -> bool:
    return all(purposes.count(item) == 1 for item in required) and [
        purposes.index(item) for item in required
    ] == sorted(purposes.index(item) for item in required)


def _new_binding_valid(
    recovery: Mapping[str, object],
    interrupted: Mapping[str, object],
    tasks: list[Mapping[str, object]],
) -> bool:
    try:
        record = EvidenceRecord.from_dict(object_mapping(recovery.get("new_binding_record")))
        binding = load_visual_binding(record)
    except (KeyError, TypeError, ValueError):
        return False
    return (
        binding.evidence_key == recovery.get("new_binding_evidence_key")
        and binding.evidence_key != recovery.get("old_binding_evidence_key")
        and binding.candidate_id == interrupted.get("source_id")
        and binding.lineage_id == interrupted.get("lineage_id")
        and all(task.get("binding_evidence_key") == binding.evidence_key for task in tasks[1:])
    )


def _trace_order_valid(
    purposes: tuple[str, ...],
    *,
    fault: str | None,
    recovery: Mapping[str, object],
    recovery_strategy: str,
) -> bool:
    first_correction = f"task-0002:servo-step-{PROFILE.fault_servo_step}"
    if purposes.count(first_correction) != 1:
        return False
    after_fault = purposes[purposes.index(first_correction) + 1 :]
    if fault == RECOVERABLE and recovery.get("status") == "validated":
        if recovery_strategy == "full_reacquisition":
            checks = tuple(f"task-0002:reacquire-fit-{index + 1}" for index in range(4)) + tuple(
                f"task-0002:reacquire-validation-{index + 1}" for index in range(2)
            )
        else:
            checks = ("task-0002:replacement-check-1", "task-0002:replacement-check-2")
        resumed = f"task-0002:resumed:servo-step-{PROFILE.fault_servo_step}"
        if not _ordered_once(after_fault, (*checks, resumed)):
            return False
        after_fault = after_fault[: after_fault.index(resumed)]
    return not any(
        purpose.startswith("task-0002:servo-step-")
        or purpose.startswith("task-0002:resumed:servo-step-")
        for purpose in after_fault
    )


def summarize_recovery_episode(
    evidence_path: Path,
    *,
    seed: int,
    fault: str | None,
    recovery_strategy: str = "checked_reuse",
    execution_path: str = "session",
) -> dict[str, object]:
    """Apply structural gates to one create-only live episode's private evidence."""

    if fault not in {None, RECOVERABLE, *INVALID_REPLACEMENTS}:
        raise ValueError("unsupported recovery evaluation fault")
    if recovery_strategy not in {"checked_reuse", "full_reacquisition"} or (
        recovery_strategy == "full_reacquisition" and fault != RECOVERABLE
    ):
        raise ValueError("unsupported recovery evaluation strategy")
    if execution_path not in {"session", "direct_handwritten"}:
        raise ValueError("unsupported recovery execution path")
    record: dict[str, object] = {
        "seed": seed,
        "fault": fault,
        "run_id": evidence_path.name,
        "evidence_path": str(evidence_path),
        "qualification_profile": {
            "profile_id": PROFILE.profile_id,
            "digest": PROFILE.digest,
        },
        "offered_tasks": len(PROFILE.base_offsets_px),
        "recovery_strategy": recovery_strategy,
        "execution_path": execution_path,
    }
    try:
        summary = read_json_object(evidence_path / "resident-demo.json")
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return {
            **record,
            "outcome": "failed",
            "reason_code": "missing_or_invalid_summary",
            "error": str(error),
        }

    identity_ok = (
        summary.get("experiment_seed") == seed
        and summary.get("target_offsets_px") == list(PROFILE.target_offsets(seed))
        and object_mapping(summary.get("qualification_profile")).get("digest") == PROFILE.digest
        and summary.get("evaluation_fault_profile") == fault
        and summary.get("world_count") == 1
        and summary.get("evaluation_recovery_strategy", "checked_reuse") == recovery_strategy
        and summary.get("execution_path", "session") == execution_path
    )
    if not identity_ok:
        return {**record, "outcome": "failed", "reason_code": "profile_or_trial_mismatch"}

    operation_ids = summary.get("operation_ids")
    if not isinstance(operation_ids, list) or any(
        not isinstance(item, str) for item in operation_ids
    ):
        return {**record, "outcome": "failed", "reason_code": "invalid_operation_ledger"}
    tasks: list[Mapping[str, object]] = []
    try:
        for index in range(1, len(operation_ids) + 1):
            tasks.append(read_json_object(evidence_path / f"task-{index:04d}.json"))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return {
            **record,
            "outcome": "failed",
            "reason_code": "missing_or_invalid_task",
            "error": str(error),
        }

    task_ids_match = all(
        task.get("operation_id") == operation_id
        for task, operation_id in zip(tasks, operation_ids, strict=True)
    )
    false_completions = sum(
        object_mapping(task.get("evaluator_only")).get("false_visual_completion") is True
        for task in tasks
    )
    passed_tasks = sum(task.get("status") == "passed" for task in tasks)
    interrupted = (
        tasks[PROFILE.fault_task_index - 1] if len(tasks) >= PROFILE.fault_task_index else {}
    )
    fault_event = object_mapping(interrupted.get("fault_evaluator_only"))
    recovery = object_mapping(interrupted.get("recovery"))
    worker = object_mapping(interrupted.get("detector_worker"))
    fault_applied = fault_event.get("applied") is True
    purposes: tuple[str, ...] = ()
    trace_order_ok = True
    if fault_applied:
        try:
            purposes = read_action_purposes(evidence_path / "task-0002-trace.jsonl")
        except (OSError, ValueError, json.JSONDecodeError):
            purposes = ()
        trace_order_ok = _trace_order_valid(
            purposes, fault=fault, recovery=recovery, recovery_strategy=recovery_strategy
        )
    measurements = {
        "episode_wall_ms": finite_float(summary.get("episode_wall_ms")),
        "container_startup_ms": finite_float(summary.get("container_startup_ms")),
        "acquisition_ms": finite_float(summary.get("acquisition_ms")),
        "fault_to_detection_ms": finite_float(fault_event.get("fault_to_detection_ms")),
        "repair_ms": finite_float(recovery.get("repair_ms")),
        "post_fault_trace_order_ok": trace_order_ok if fault_applied else None,
        "total_validation_probes": sum(
            nonnegative_int(task.get("validation_probe_count")) for task in tasks
        ),
        "replacement_validation_probes": recovery.get("validation_probe_count"),
        "recovery_identification_probes": recovery.get("identification_probe_count"),
        "completed_reacquisition_fit_probes": sum(
            f"task-0002:reacquire-fit-{index + 1}" in purposes for index in range(4)
        ),
        "completed_reacquisition_validation_probes": sum(
            f"task-0002:reacquire-validation-{index + 1}" in purposes for index in range(2)
        ),
        "commanded_travel_radians": round(
            sum(finite_float(task.get("commanded_travel_radians")) or 0 for task in tasks), 6
        ),
    }
    structural = (
        task_ids_match
        and trace_order_ok
        and len(set(operation_ids)) == len(operation_ids)
        and false_completions == 0
        and all(task.get("quiescence_confirmed") is True for task in tasks)
        and (execution_path != "direct_handwritten" or summary.get("world_stop_confirmed") is True)
        and not (recovery.get("status") == "unavailable" and any(evidence_path.glob("task-0003*")))
    )
    reuse_method_ok = (
        recovery.get("strategy", "checked_reuse") == "checked_reuse"
        and object_mapping(recovery.get("validation")).get("status") == "reused"
        and recovery.get("validation_probe_count") == len(PROFILE.replacement_probes_radians)
    )
    full_method_ok = (
        recovery.get("strategy") == "full_reacquisition"
        and object_mapping(recovery.get("validation")).get("status") == "passed"
        and recovery.get("identification_probe_count") == 4
        and recovery.get("validation_probe_count") == 2
        and bool(tasks)
        and recovery.get("old_binding_evidence_key") == tasks[0].get("binding_evidence_key")
        and _new_binding_valid(recovery, interrupted, tasks)
    )
    method_ok = full_method_ok if recovery_strategy == "full_reacquisition" else reuse_method_ok
    if fault is None:
        accepted = (
            structural
            and len(tasks) == len(PROFILE.base_offsets_px)
            and summary.get("status") == "passed"
            and passed_tasks == len(tasks)
            and all(
                object_mapping(task.get("evaluator_only")).get("independently_inside_tolerance")
                is True
                for task in tasks
            )
            and not any(
                object_mapping(task.get("fault_evaluator_only")).get("applied") is True
                for task in tasks
            )
        )
    elif fault == RECOVERABLE:
        accepted = (
            structural
            and len(tasks) == len(PROFILE.base_offsets_px)
            and summary.get("status") == "passed"
            and passed_tasks == len(tasks)
            and fault_applied
            and fault_event.get("phase") == PROFILE.fault_phase
            and recovery.get("status") == "validated"
            and recovery.get("old_binding_revision_invalidated") is True
            and object_mapping(recovery.get("quiescence")).get("confirmed") is True
            and method_ok
            and interrupted.get("binding_revision") == 2
            and worker.get("worker_generation") == 2
            and worker.get("worker_instance") != fault_event.get("primary_instance")
            and all(
                object_mapping(task.get("evaluator_only")).get("independently_inside_tolerance")
                is True
                for task in tasks
            )
        )
    else:
        accepted = (
            structural
            and len(tasks) == PROFILE.fault_task_index
            and summary.get("status") == "failed"
            and passed_tasks == PROFILE.fault_task_index - 1
            and fault_applied
            and fault_event.get("phase") == PROFILE.fault_phase
            and interrupted.get("status") == "failed"
            and interrupted.get("reason_code") == "VISUAL_CAPABILITY_UNAVAILABLE"
            and interrupted.get("final_y_px") is None
            and interrupted.get("visual_capability_available") is False
            and recovery.get("status") == "unavailable"
            and not any(evidence_path.glob("task-0003*"))
        )
    if accepted:
        reason = None
    elif fault is not None and (
        len(tasks) < PROFILE.fault_task_index or fault_event.get("applied") is False
    ):
        reason = "pre_trigger_failure"
    elif fault is not None and fault_event.get("applied") is None:
        reason = "fault_application_unknown"
    elif (
        structural
        and recovery_strategy == "full_reacquisition"
        and recovery.get("failure_reason") == "REPAIR_BUDGET_EXHAUSTED"
        and recovery.get("status") == "unavailable"
    ):
        reason = "repair_budget_exhausted"
    elif structural and summary.get("status") == "failed":
        reason = "task_not_completed"
    else:
        reason = "structural_gate_failed"
    return {
        **record,
        "outcome": "passed" if accepted else "failed",
        "reason_code": reason,
        "reported_status": summary.get("status"),
        "admitted_tasks": len(tasks),
        "passed_tasks": passed_tasks,
        "fault_applied": fault_applied,
        "false_completion_count": false_completions,
        "structural_gates_passed": structural,
        "measurements": measurements,
    }
