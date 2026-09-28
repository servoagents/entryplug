from __future__ import annotations

import json
from pathlib import Path

import pytest

from entryplug.cli import _parse_args
from entryplug_evaluation.harbor.quiescence_loss import score_quiescence_loss
from entryplug_harbor.recovery_qualification import HARBOR_RESIDENT_RECOVERY_V1 as PROFILE
from entryplug_harbor.resident import QUIESCENCE_LOSS_FAULT, SOURCE_ID, SOURCE_LINEAGE

FIRST = "a" * 32
SECOND = "b" * 32


def _write(path: Path, value: dict[str, object]) -> None:
    path.write_text(json.dumps(value) + "\n", encoding="utf-8")


def _episode(path: Path) -> None:
    path.mkdir()
    _write(
        path / "resident-quiescence-loss.json",
        {
            "experiment_seed": 303,
            "initial_y_px": 240.0,
            "target_offsets_px": list(PROFILE.target_offsets(303)),
            "qualification_profile": {"digest": PROFILE.digest},
            "evaluation_fault_profile": QUIESCENCE_LOSS_FAULT,
            "world_count": 1,
            "world_stop_confirmed": True,
            "motion_inhibited_reason": "STOP_UNCONFIRMED",
            "third_admission_rejection": "MOTION_INHIBITED",
            "operations": [
                {"operation_id": FIRST, "lifecycle": "succeeded"},
                {
                    "operation_id": SECOND,
                    "lifecycle": "indeterminate",
                    "motion_state": "unknown",
                    "reason_code": "STOP_UNCONFIRMED",
                },
            ],
            "error": None,
        },
    )
    _write(
        path / "task-0001.json",
        {
            "operation_id": FIRST,
            "status": "passed",
            "evaluator_only": {"false_visual_completion": False},
        },
    )
    _write(
        path / "task-0002.json",
        {
            "operation_id": SECOND,
            "status": "failed",
            "quiescence_confirmed": False,
            "binding_ready": False,
            "quiescence_evidence": {
                "confirmed": False,
                "reason_code": "PUBLIC_JOINT_FEEDBACK_UNCONFIRMED",
                "error_type": "TimeoutError",
            },
            "recovery": {"status": "unavailable", "failure_reason": "QUIESCENCE_UNCONFIRMED"},
            "fault_evaluator_only": {"applied": True, "phase": PROFILE.fault_phase},
            "evaluator_only": {"false_visual_completion": False},
        },
    )
    _write(path / f"goal-{FIRST}.json", {"operation_id": FIRST})
    _write(
        path / f"goal-{SECOND}.json",
        {
            "operation_id": SECOND,
            "source_id": SOURCE_ID,
            "lineage_id": SOURCE_LINEAGE,
            "target_y_px": 240.0 + PROFILE.target_offsets(303)[1],
        },
    )
    _write(
        path / "task-0002-repair-started.evaluator.json",
        {"operation_id": SECOND, "fault_phase": PROFILE.fault_phase, "detected_monotonic": 10},
    )
    _write(
        path / "task-0002-joint-feedback-lost.evaluator.json",
        {
            "operation_id": SECOND,
            "fault_phase": PROFILE.fault_phase,
            "last_joint_sequence": 50,
            "loss_monotonic": 11,
            "topic": "/joint_states",
        },
    )
    (path / "task-0002-trace.jsonl").write_text(
        json.dumps({"purpose": f"task-0002:servo-step-{PROFILE.fault_servo_step}"}) + "\n",
        encoding="utf-8",
    )


def test_quiescence_loss_requires_motion_fence_and_no_repair_probe(tmp_path: Path) -> None:
    run = tmp_path / "feedback-loss"
    _episode(run)
    score = score_quiescence_loss(run, seed=303)
    assert score["safety_outcome"] == "passed"
    assert score["host_motion_inhibited"] is True
    assert score["no_repair_motion"] is True


@pytest.mark.parametrize(
    "violation",
    (
        "claimed_hold",
        "missing_fence",
        "third_admitted",
        "repair_probe",
        "repair_goal_marker",
        "third_goal",
        "wrong_profile",
        "no_world_stop",
        "wrong_feedback_topic",
        "loss_before_detection",
        "unsafe_operation_id",
    ),
)
def test_quiescence_loss_rejects_broken_evidence(tmp_path: Path, violation: str) -> None:
    run = tmp_path / "feedback-loss"
    _episode(run)
    if violation in {
        "missing_fence",
        "third_admitted",
        "wrong_profile",
        "no_world_stop",
        "unsafe_operation_id",
    }:
        file = run / "resident-quiescence-loss.json"
        summary = json.loads(file.read_text(encoding="utf-8"))
        if violation == "missing_fence":
            summary["motion_inhibited_reason"] = None
        elif violation == "third_admitted":
            summary["third_admission_rejection"] = None
        elif violation == "wrong_profile":
            summary["qualification_profile"]["digest"] = "wrong"
        elif violation == "no_world_stop":
            summary["world_stop_confirmed"] = False
        else:
            summary["operations"][1]["operation_id"] = "../../outside"
        _write(file, summary)
    elif violation == "claimed_hold":
        file = run / "task-0002.json"
        task = json.loads(file.read_text(encoding="utf-8"))
        task["quiescence_confirmed"] = True
        _write(file, task)
    elif violation == "repair_probe":
        with (run / "task-0002-trace.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"purpose": "task-0002:replacement-check-1"}) + "\n")
    elif violation == "repair_goal_marker":
        _write(run / "task-0002-repair-goal-accepted.evaluator.json", {})
    elif violation == "third_goal":
        _write(run / f"goal-{'c' * 32}.json", {})
    else:
        file = run / "task-0002-joint-feedback-lost.evaluator.json"
        marker = json.loads(file.read_text(encoding="utf-8"))
        if violation == "wrong_feedback_topic":
            marker["topic"] = "/camera/color/image_raw"
        else:
            marker["loss_monotonic"] = 9
        _write(file, marker)
    assert score_quiescence_loss(run, seed=303)["safety_outcome"] == "failed"


def test_quiescence_loss_stays_development_only(tmp_path: Path) -> None:
    run = tmp_path / "feedback-loss"
    _episode(run)
    with pytest.raises(ValueError, match="development seeds only"):
        score_quiescence_loss(run, seed=PROFILE.holdout_seeds[0])
    assert (
        _parse_args(
            ["resident-demo", "--runtime", "container", "--fault", QUIESCENCE_LOSS_FAULT]
        ).fault
        == QUIESCENCE_LOSS_FAULT
    )
