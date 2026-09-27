from __future__ import annotations

import json
from pathlib import Path

from entryplug.association import make_visual_binding_record
from entryplug.recovery_evaluation import summarize_recovery_episode
from entryplug.recovery_qualification import HARBOR_RESIDENT_RECOVERY_V1 as PROFILE


def _write(path: Path, value: dict[str, object]) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def _episode(path: Path, *, fault: str | None, count: int = 4) -> None:
    path.mkdir()
    operation_ids = [f"op-{index}" for index in range(1, count + 1)]
    _write(
        path / "resident-demo.json",
        {
            "status": "passed" if count == 4 else "failed",
            "experiment_seed": 101,
            "target_offsets_px": list(PROFILE.target_offsets(101)),
            "qualification_profile": {"profile_id": PROFILE.profile_id, "digest": PROFILE.digest},
            "evaluation_fault_profile": fault,
            "world_count": 1,
            "operation_ids": operation_ids,
            "episode_wall_ms": 1000,
        },
    )
    for index, operation_id in enumerate(operation_ids, start=1):
        task: dict[str, object] = {
            "operation_id": operation_id,
            "status": "passed",
            "reason_code": "TARGET_REACHED",
            "quiescence_confirmed": True,
            "fault_evaluator_only": {"applied": False},
            "evaluator_only": {
                "independently_inside_tolerance": True,
                "false_visual_completion": False,
            },
            "validation_probe_count": 2,
            "commanded_travel_radians": 0.2,
        }
        if index == 2 and fault is not None:
            task["fault_evaluator_only"] = {
                "applied": True,
                "phase": PROFILE.fault_phase,
                "primary_instance": "old",
                "fault_to_detection_ms": 50,
            }
            if fault == "kill-active-worker":
                task.update(
                    recovery={
                        "status": "validated",
                        "old_binding_revision_invalidated": True,
                        "quiescence": {"confirmed": True},
                        "validation": {"status": "reused"},
                        "validation_probe_count": 2,
                        "repair_ms": 300,
                    },
                    binding_revision=2,
                    detector_worker={"worker_generation": 2, "worker_instance": "new"},
                )
            else:
                task.update(
                    status="failed",
                    reason_code="VISUAL_CAPABILITY_UNAVAILABLE",
                    final_y_px=None,
                    visual_capability_available=False,
                    recovery={"status": "unavailable"},
                    evaluator_only={
                        "independently_inside_tolerance": False,
                        "false_visual_completion": False,
                    },
                )
        _write(path / f"task-{index:04d}.json", task)

    if fault is not None and count >= 2:
        purposes = [f"task-0002:servo-step-{PROFILE.fault_servo_step}"]
        if fault == "kill-active-worker":
            purposes.extend(
                (
                    "task-0002:replacement-check-1",
                    "task-0002:replacement-check-2",
                    f"task-0002:resumed:servo-step-{PROFILE.fault_servo_step}",
                )
            )
        with (path / "task-0002-trace.jsonl").open("x", encoding="utf-8") as stream:
            for purpose in purposes:
                stream.write(json.dumps({"purpose": purpose}) + "\n")


def test_recovery_evaluator_accepts_verified_repair_and_no_fault(tmp_path: Path) -> None:
    recovered = tmp_path / "recovered"
    _episode(recovered, fault="kill-active-worker")
    result = summarize_recovery_episode(recovered, seed=101, fault="kill-active-worker")
    assert result["outcome"] == "passed"
    assert result["admitted_tasks"] == result["offered_tasks"] == 4
    assert result["measurements"]["replacement_validation_probes"] == 2

    no_fault = tmp_path / "no-fault"
    _episode(no_fault, fault=None)
    assert summarize_recovery_episode(no_fault, seed=101, fault=None)["outcome"] == "passed"


def test_recovery_evaluator_counts_honest_unavailability(tmp_path: Path) -> None:
    for fault in ("kill-both-workers", "kill-active-worker-stale-alternate"):
        path = tmp_path / fault
        _episode(path, fault=fault, count=2)
        result = summarize_recovery_episode(path, seed=101, fault=fault)
        assert result["outcome"] == "passed"
        assert result["admitted_tasks"] == 2
        assert result["passed_tasks"] == 1
        _write(path / "task-0003.json", {})
        assert summarize_recovery_episode(path, seed=101, fault=fault)["outcome"] == "failed"


def test_recovery_evaluator_rejects_correction_after_unavailability(tmp_path: Path) -> None:
    path = tmp_path / "unsafe-negative"
    _episode(path, fault="kill-both-workers", count=2)
    with (path / "task-0002-trace.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"purpose": "task-0002:resumed:servo-step-1"}) + "\n")
    result = summarize_recovery_episode(path, seed=101, fault="kill-both-workers")
    assert result["outcome"] == "failed"
    assert result["measurements"]["post_fault_trace_order_ok"] is False


def test_recovery_evaluator_does_not_hide_pre_trigger_failure(tmp_path: Path) -> None:
    path = tmp_path / "failed-before-fault"
    _episode(path, fault="kill-active-worker", count=1)
    result = summarize_recovery_episode(path, seed=101, fault="kill-active-worker")
    assert result["outcome"] == "failed"
    assert result["reason_code"] == "pre_trigger_failure"
    assert result["offered_tasks"] == 4
    assert result["fault_applied"] is False


def test_recovery_evaluator_marks_unknown_fault_application(tmp_path: Path) -> None:
    path = tmp_path / "fault-unknown"
    _episode(path, fault="kill-active-worker", count=2)
    artifact = path / "task-0002.json"
    task = json.loads(artifact.read_text(encoding="utf-8"))
    task["fault_evaluator_only"]["applied"] = None
    _write(artifact, task)
    result = summarize_recovery_episode(path, seed=101, fault="kill-active-worker")
    assert result["outcome"] == "failed"
    assert result["reason_code"] == "fault_application_unknown"


def test_recovery_evaluator_rejects_profile_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "wrong-seed"
    _episode(path, fault=None)
    assert summarize_recovery_episode(path, seed=202, fault=None)["reason_code"] == (
        "profile_or_trial_mismatch"
    )


def test_full_reacquisition_requires_new_content_addressed_binding(tmp_path: Path) -> None:
    path = tmp_path / "same-world-reacquisition"
    _episode(path, fault="kill-active-worker")
    summary_path = path / "resident-demo.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["evaluation_recovery_strategy"] = "full_reacquisition"
    _write(summary_path, summary)
    record = make_visual_binding_record(
        context={"task": "visual_reach", "recovery_strategy": "full_reacquisition"},
        candidate_id="camera-color-v1",
        lineage_id="camera-color-capture-v1",
        gain_px_per_radian=95.0,
        validity_radians=(-0.045, 0.05),
        noise_range_px=0.5,
        maximum_age_ms=250.0,
        timing_method_id="settled-before-after-v1",
        evidence_refs=("task-0002.json", "task-0002-trace.jsonl"),
        created_at="2026-09-27T00:00:00+00:00",
    )
    for index in range(1, 5):
        task_path = path / f"task-{index:04d}.json"
        task = json.loads(task_path.read_text(encoding="utf-8"))
        task["binding_evidence_key"] = "sha256:old" if index == 1 else record.key
        if index == 2:
            task["source_id"] = "camera-color-v1"
            task["lineage_id"] = "camera-color-capture-v1"
            task["recovery"].update(
                strategy="full_reacquisition",
                validation={"status": "passed"},
                identification_probe_count=4,
                validation_probe_count=2,
                old_binding_evidence_key="sha256:old",
                new_binding_evidence_key=record.key,
                new_binding_record=record.to_dict(),
            )
        _write(task_path, task)
    purposes = ["task-0002:servo-step-1"]
    purposes.extend(f"task-0002:reacquire-fit-{index}" for index in range(1, 5))
    purposes.extend(f"task-0002:reacquire-validation-{index}" for index in range(1, 3))
    purposes.append("task-0002:resumed:servo-step-1")
    with (path / "task-0002-trace.jsonl").open("w", encoding="utf-8") as stream:
        for purpose in purposes:
            stream.write(json.dumps({"purpose": purpose}) + "\n")
    scored = summarize_recovery_episode(
        path, seed=101, fault="kill-active-worker", recovery_strategy="full_reacquisition"
    )
    assert scored["outcome"] == "passed"
    assert scored["measurements"]["recovery_identification_probes"] == 4
    interrupted_path = path / "task-0002.json"
    interrupted = json.loads(interrupted_path.read_text(encoding="utf-8"))
    interrupted["recovery"]["new_binding_record"]["payload"]["maximum_age_ms"] = 999.0
    _write(interrupted_path, interrupted)
    assert (
        summarize_recovery_episode(
            path, seed=101, fault="kill-active-worker", recovery_strategy="full_reacquisition"
        )["outcome"]
        == "failed"
    )


def test_full_reacquisition_budget_exhaustion_is_safe_but_not_success(
    tmp_path: Path,
) -> None:
    path = tmp_path / "budget-exhausted"
    _episode(path, fault="kill-active-worker", count=2)
    summary_path = path / "resident-demo.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["evaluation_recovery_strategy"] = "full_reacquisition"
    _write(summary_path, summary)
    task_path = path / "task-0002.json"
    task = json.loads(task_path.read_text(encoding="utf-8"))
    task.update(
        status="failed",
        reason_code="VISUAL_CAPABILITY_UNAVAILABLE",
        final_y_px=None,
        recovery={
            "status": "unavailable",
            "strategy": "full_reacquisition",
            "failure_reason": "REPAIR_BUDGET_EXHAUSTED",
        },
    )
    _write(task_path, task)
    trace_path = path / "task-0002-trace.jsonl"
    with trace_path.open("w", encoding="utf-8") as stream:
        for purpose in (
            "task-0002:servo-step-1",
            "task-0002:reacquire-fit-1",
            "task-0002:reacquire-fit-2",
        ):
            stream.write(json.dumps({"purpose": purpose}) + "\n")

    scored = summarize_recovery_episode(
        path, seed=101, fault="kill-active-worker", recovery_strategy="full_reacquisition"
    )
    assert scored["outcome"] == "failed"
    assert scored["reason_code"] == "repair_budget_exhausted"
    assert scored["structural_gates_passed"] is True
    assert scored["measurements"]["completed_reacquisition_fit_probes"] == 2

    with trace_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"purpose": "task-0002:resumed:servo-step-1"}) + "\n")
    unsafe = summarize_recovery_episode(
        path, seed=101, fault="kill-active-worker", recovery_strategy="full_reacquisition"
    )
    assert unsafe["structural_gates_passed"] is False
    assert unsafe["reason_code"] == "structural_gate_failed"


def test_recovery_evaluator_rejects_old_correction_before_revalidation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "premature-correction"
    _episode(path, fault="kill-active-worker")
    trace_path = path / "task-0002-trace.jsonl"
    purposes = [
        "task-0002:servo-step-1",
        "task-0002:servo-step-2",
        "task-0002:replacement-check-1",
        "task-0002:replacement-check-2",
        "task-0002:resumed:servo-step-1",
    ]
    with trace_path.open("w", encoding="utf-8") as stream:
        for purpose in purposes:
            stream.write(json.dumps({"purpose": purpose}) + "\n")
    result = summarize_recovery_episode(path, seed=101, fault="kill-active-worker")
    assert result["outcome"] == "failed"
    assert result["structural_gates_passed"] is False
