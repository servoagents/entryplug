"""Reduce every offered resident task, including pre-fault failures."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from entryplug.association import load_visual_binding
from entryplug.evidence import EvidenceRecord
from entryplug.recovery_qualification import HARBOR_RESIDENT_RECOVERY_V1 as PROFILE

RECOVERABLE = "kill-active-worker"
INVALID_REPLACEMENTS = frozenset({"kill-both-workers", "kill-active-worker-stale-alternate"})


def _object(path: Path) -> Mapping[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} is not a JSON object")
    return value


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _integer(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _trace_purposes(path: Path) -> tuple[str, ...]:
    purposes: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if not isinstance(record, dict) or not isinstance(record.get("purpose"), str):
            raise ValueError("native action trace has an invalid purpose")
        purposes.append(record["purpose"])
    if not purposes:
        raise ValueError("native action trace is empty")
    return tuple(purposes)


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
        record = EvidenceRecord.from_dict(_mapping(recovery.get("new_binding_record")))
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


def summarize_recovery_episode(
    evidence_path: Path,
    *,
    seed: int,
    fault: str | None,
    recovery_strategy: str = "checked_reuse",
) -> dict[str, object]:
    """Apply structural gates to one create-only live episode's private evidence."""

    if fault not in {None, RECOVERABLE, *INVALID_REPLACEMENTS}:
        raise ValueError("unsupported recovery evaluation fault")
    if recovery_strategy not in {"checked_reuse", "full_reacquisition"} or (
        recovery_strategy == "full_reacquisition" and fault != RECOVERABLE
    ):
        raise ValueError("unsupported recovery evaluation strategy")
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
    }
    try:
        summary = _object(evidence_path / "resident-demo.json")
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
        and _mapping(summary.get("qualification_profile")).get("digest") == PROFILE.digest
        and summary.get("evaluation_fault_profile") == fault
        and summary.get("world_count") == 1
        and summary.get("evaluation_recovery_strategy", "checked_reuse") == recovery_strategy
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
            tasks.append(_object(evidence_path / f"task-{index:04d}.json"))
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
        _mapping(task.get("evaluator_only")).get("false_visual_completion") is True
        for task in tasks
    )
    passed_tasks = sum(task.get("status") == "passed" for task in tasks)
    interrupted = (
        tasks[PROFILE.fault_task_index - 1] if len(tasks) >= PROFILE.fault_task_index else {}
    )
    fault_event = _mapping(interrupted.get("fault_evaluator_only"))
    recovery = _mapping(interrupted.get("recovery"))
    worker = _mapping(interrupted.get("detector_worker"))
    fault_applied = fault_event.get("applied") is True
    purposes: tuple[str, ...] = ()
    trace_order_ok = True
    if fault_applied:
        try:
            purposes = _trace_purposes(evidence_path / "task-0002-trace.jsonl")
        except (OSError, ValueError, json.JSONDecodeError):
            purposes = ()
        first_correction = f"task-0002:servo-step-{PROFILE.fault_servo_step}"
        trace_order_ok = purposes.count(first_correction) == 1
        if trace_order_ok:
            after_fault = purposes[purposes.index(first_correction) + 1 :]
            if fault == RECOVERABLE and recovery.get("status") == "validated":
                if recovery_strategy == "full_reacquisition":
                    checks = tuple(
                        f"task-0002:reacquire-fit-{index + 1}" for index in range(4)
                    ) + tuple(f"task-0002:reacquire-validation-{index + 1}" for index in range(2))
                else:
                    checks = (
                        "task-0002:replacement-check-1",
                        "task-0002:replacement-check-2",
                    )
                resumed = f"task-0002:resumed:servo-step-{PROFILE.fault_servo_step}"
                trace_order_ok = _ordered_once(after_fault, (*checks, resumed))
                if trace_order_ok:
                    before_resume = after_fault[: after_fault.index(resumed)]
                    trace_order_ok = not any(
                        purpose.startswith("task-0002:servo-step-")
                        or purpose.startswith("task-0002:resumed:servo-step-")
                        for purpose in before_resume
                    )
            else:
                trace_order_ok = not any(
                    purpose.startswith("task-0002:resumed:servo-step-")
                    or purpose.startswith("task-0002:servo-step-")
                    for purpose in after_fault
                )
    measurements = {
        "episode_wall_ms": _number(summary.get("episode_wall_ms")),
        "container_startup_ms": _number(summary.get("container_startup_ms")),
        "acquisition_ms": _number(summary.get("acquisition_ms")),
        "fault_to_detection_ms": _number(fault_event.get("fault_to_detection_ms")),
        "repair_ms": _number(recovery.get("repair_ms")),
        "post_fault_trace_order_ok": trace_order_ok if fault_applied else None,
        "total_validation_probes": sum(
            _integer(task.get("validation_probe_count")) for task in tasks
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
            sum(_number(task.get("commanded_travel_radians")) or 0 for task in tasks), 6
        ),
    }
    structural = (
        task_ids_match
        and trace_order_ok
        and len(set(operation_ids)) == len(operation_ids)
        and false_completions == 0
        and all(task.get("quiescence_confirmed") is True for task in tasks)
        and not (recovery.get("status") == "unavailable" and any(evidence_path.glob("task-0003*")))
    )
    reuse_method_ok = (
        recovery.get("strategy", "checked_reuse") == "checked_reuse"
        and _mapping(recovery.get("validation")).get("status") == "reused"
        and recovery.get("validation_probe_count") == len(PROFILE.replacement_probes_radians)
    )
    full_method_ok = (
        recovery.get("strategy") == "full_reacquisition"
        and _mapping(recovery.get("validation")).get("status") == "passed"
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
                _mapping(task.get("evaluator_only")).get("independently_inside_tolerance") is True
                for task in tasks
            )
            and not any(
                _mapping(task.get("fault_evaluator_only")).get("applied") is True for task in tasks
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
            and _mapping(recovery.get("quiescence")).get("confirmed") is True
            and method_ok
            and interrupted.get("binding_revision") == 2
            and worker.get("worker_generation") == 2
            and worker.get("worker_instance") != fault_event.get("primary_instance")
            and all(
                _mapping(task.get("evaluator_only")).get("independently_inside_tolerance") is True
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
