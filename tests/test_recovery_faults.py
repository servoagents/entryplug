from __future__ import annotations

import json
from pathlib import Path

import pytest

from entryplug.harbor_resident import RESULT_LOSS_FAULT, SOURCE_ID, SOURCE_LINEAGE
from entryplug.recovery_faults import record_repair_result_loss, score_repair_result_loss
from entryplug.recovery_qualification import HARBOR_RESIDENT_RECOVERY_V1 as PROFILE


def _write(path: Path, value: dict[str, object]) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def _episode(path: Path) -> None:
    path.mkdir()
    _write(
        path / "resident-demo.json",
        {
            "experiment_seed": 101,
            "target_offsets_px": list(PROFILE.target_offsets(101)),
            "qualification_profile": {"digest": PROFILE.digest},
            "evaluation_fault_profile": RESULT_LOSS_FAULT,
            "world_count": 1,
            "status": "failed",
            "operation_ids": ["first", "second"],
            "outcomes": [
                {"lifecycle": "succeeded", "reason_code": None, "result": {}},
                {
                    "lifecycle": "failed",
                    "reason_code": "REPLY_LOST_STOPPED",
                    "result": {"quiescence_confirmed": True},
                },
            ],
        },
    )
    _write(
        path / "task-0001.json",
        {
            "operation_id": "first",
            "status": "passed",
            "source_id": SOURCE_ID,
            "lineage_id": SOURCE_LINEAGE,
        },
    )
    _write(
        path / "task-0002.json",
        {
            "operation_id": "second",
            "source_id": SOURCE_ID,
            "lineage_id": SOURCE_LINEAGE,
            "status": "failed",
            "reason_code": "EVALUATOR_RESULT_DROPPED_BEFORE_RESUME",
            "evaluator_only_result_drop": True,
            "fault_evaluator_only": {
                "applied": True,
                "phase": PROFILE.fault_phase,
                "primary_instance": "old",
                "result_drop_phase": "replacement_validated_before_resume",
            },
            "recovery": {
                "status": "validated",
                "old_binding_revision_invalidated": True,
                "quiescence": {"confirmed": True},
                "validation": {"status": "reused"},
                "validation_probe_count": len(PROFILE.replacement_probes_radians),
            },
            "binding_revision": 2,
            "detector_worker": {"worker_generation": 2, "worker_instance": "new"},
            "quiescence_confirmed": True,
            "evaluator_only": {"false_visual_completion": False},
        },
    )
    with (path / "task-0002-trace.jsonl").open("x", encoding="utf-8") as stream:
        for purpose in (
            f"task-0002:servo-step-{PROFILE.fault_servo_step}",
            "task-0002:replacement-check-1",
            "task-0002:replacement-check-2",
        ):
            stream.write(json.dumps({"purpose": purpose}) + "\n")


def test_repair_result_loss_requires_honest_failure_after_validated_checks(tmp_path: Path) -> None:
    run = tmp_path / "result-loss"
    _episode(run)
    report = score_repair_result_loss(run, seed=101)
    assert report["safety_outcome"] == "passed"
    assert report["task_outcome"] == "failed"
    assert report["host_stop_confirmed"] is True
    assert report["no_resumed_correction"] is True


@pytest.mark.parametrize(
    "violation",
    ["continued_motion", "third_task", "false_success", "unconfirmed_stop", "wrong_profile"],
)
def test_repair_result_loss_rejects_unsafe_or_misidentified_evidence(
    tmp_path: Path, violation: str
) -> None:
    run = tmp_path / "result-loss"
    _episode(run)
    if violation == "continued_motion":
        with (run / "task-0002-trace.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"purpose": "task-0002:resumed:servo-step-1"}) + "\n")
    elif violation == "third_task":
        _write(run / "task-0003.json", {})
    elif violation == "false_success":
        artifact = run / "task-0002.json"
        task = json.loads(artifact.read_text(encoding="utf-8"))
        task["evaluator_only"]["false_visual_completion"] = True
        _write(artifact, task)
    elif violation == "unconfirmed_stop":
        artifact = run / "resident-demo.json"
        summary = json.loads(artifact.read_text(encoding="utf-8"))
        summary["outcomes"][1]["result"]["quiescence_confirmed"] = False
        _write(artifact, summary)
    else:
        artifact = run / "resident-demo.json"
        summary = json.loads(artifact.read_text(encoding="utf-8"))
        summary["qualification_profile"]["digest"] = "wrong"
        _write(artifact, summary)
    assert score_repair_result_loss(run, seed=101)["safety_outcome"] == "failed"


def test_repair_result_loss_rejects_holdout_seed_without_scoring(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="development seeds only"):
        score_repair_result_loss(tmp_path / "unused", seed=PROFILE.holdout_seeds[0])


def test_repair_result_loss_record_is_create_only(tmp_path: Path) -> None:
    run = tmp_path / "result-loss"
    _episode(run)
    path = record_repair_result_loss(run, seed=101)
    assert json.loads(path.read_text(encoding="utf-8"))["safety_outcome"] == "passed"
    with pytest.raises(FileExistsError):
        record_repair_result_loss(run, seed=101)
