from __future__ import annotations

import json
from pathlib import Path

import pytest

from entryplug.cli import _parse_args
from entryplug_evaluation.harbor.native_result_loss import (
    record_native_result_loss,
    score_native_result_loss,
)
from entryplug_harbor.recovery_qualification import HARBOR_RESIDENT_RECOVERY_V1 as PROFILE
from entryplug_harbor.resident import NATIVE_RESULT_LOSS_FAULT, SOURCE_ID, SOURCE_LINEAGE

FIRST = "a" * 32
SECOND = "b" * 32
GOAL = "native-goal"


def _write(path: Path, value: dict[str, object]) -> None:
    path.write_text(json.dumps(value) + "\n", encoding="utf-8")


def _episode(path: Path) -> None:
    path.mkdir()
    _write(
        path / "resident-demo.json",
        {
            "experiment_seed": 101,
            "target_offsets_px": list(PROFILE.target_offsets(101)),
            "qualification_profile": {"digest": PROFILE.digest},
            "evaluation_fault_profile": NATIVE_RESULT_LOSS_FAULT,
            "evaluation_recovery_strategy": "checked_reuse",
            "world_count": 1,
            "status": "failed",
            "operation_ids": [FIRST, SECOND],
            "initial_y_px": 240.0,
            "outcomes": [
                {"operation_id": FIRST, "lifecycle": "succeeded"},
                {
                    "operation_id": SECOND,
                    "lifecycle": "failed",
                    "reason_code": "REPLY_LOST_STOPPED",
                    "result": {"quiescence_confirmed": True, "run_id": path.name},
                },
            ],
        },
    )
    _write(
        path / "task-0001.json",
        {
            "operation_id": FIRST,
            "status": "passed",
            "evaluator_only": {
                "independently_inside_tolerance": True,
                "false_visual_completion": False,
            },
        },
    )
    _write(path / f"goal-{FIRST}.json", {"operation_id": FIRST})
    _write(
        path / f"goal-{SECOND}.json",
        {
            "operation_id": SECOND,
            "source_id": SOURCE_ID,
            "lineage_id": SOURCE_LINEAGE,
            "target_y_px": 240.0 + PROFILE.target_offsets(101)[1],
        },
    )
    _write(
        path / "task-0002-repair-started.evaluator.json",
        {
            "operation_id": SECOND,
            "fault_phase": PROFILE.fault_phase,
            "detected_monotonic": 10.0,
        },
    )
    _write(
        path / "task-0002-repair-goal-accepted.evaluator.json",
        {"operation_id": SECOND, "goal_id": GOAL, "accepted_monotonic": 11.0},
    )
    _write(
        path / "task-0002-native-result-unobserved.evaluator.json",
        {
            "operation_id": SECOND,
            "goal_id": GOAL,
            "fault_applied": True,
            "fault_phase": PROFILE.fault_phase,
            "primary_instance": "dead-primary",
            "client_exit_monotonic": 12.0,
            "mechanism": "client_exit_after_goal_acceptance",
        },
    )


def test_native_result_loss_requires_host_stop_and_no_task_result(tmp_path: Path) -> None:
    run = tmp_path / "native-loss"
    _episode(run)
    score = score_native_result_loss(run, seed=101)
    assert score["safety_outcome"] == "passed"
    assert score["host_stop_confirmed"] is True
    assert score["no_task_result_or_retry"] is True


@pytest.mark.parametrize(
    "violation",
    (
        "unconfirmed_stop",
        "missing_goal",
        "goal_mismatch",
        "result_fabricated",
        "third_task",
        "third_goal_only",
        "wrong_profile",
        "exit_before_admission",
        "path_traversal",
    ),
)
def test_native_result_loss_rejects_broken_safety_evidence(tmp_path: Path, violation: str) -> None:
    run = tmp_path / "native-loss"
    _episode(run)
    if violation in {"unconfirmed_stop", "wrong_profile", "path_traversal"}:
        path = run / "resident-demo.json"
        summary = json.loads(path.read_text(encoding="utf-8"))
        if violation == "unconfirmed_stop":
            summary["outcomes"][1]["result"]["quiescence_confirmed"] = False
        elif violation == "wrong_profile":
            summary["qualification_profile"]["digest"] = "wrong"
        else:
            summary["operation_ids"][1] = "../../outside"
        _write(path, summary)
    elif violation == "missing_goal":
        (run / f"goal-{SECOND}.json").unlink()
    elif violation == "goal_mismatch":
        path = run / "task-0002-repair-goal-accepted.evaluator.json"
        _write(path, {"operation_id": SECOND, "goal_id": "other", "accepted_monotonic": 11.0})
    elif violation == "result_fabricated":
        _write(run / "task-0002.json", {"status": "passed"})
    elif violation == "third_goal_only":
        _write(run / f"goal-{'c' * 32}.json", {"operation_id": "c" * 32})
    elif violation == "third_task":
        _write(run / "task-0003.json", {})
    else:
        path = run / "task-0002-native-result-unobserved.evaluator.json"
        lost = json.loads(path.read_text(encoding="utf-8"))
        lost["client_exit_monotonic"] = 9.0
        _write(path, lost)
    assert score_native_result_loss(run, seed=101)["safety_outcome"] == "failed"


def test_native_result_loss_records_create_only_and_development_only(tmp_path: Path) -> None:
    run = tmp_path / "native-loss"
    _episode(run)
    path = record_native_result_loss(run, seed=101)
    assert json.loads(path.read_text(encoding="utf-8"))["safety_outcome"] == "passed"
    with pytest.raises(FileExistsError):
        record_native_result_loss(run, seed=101)
    with pytest.raises(ValueError, match="development seeds only"):
        score_native_result_loss(run, seed=PROFILE.holdout_seeds[0])
    args = _parse_args(
        ["resident-demo", "--runtime", "container", "--fault", NATIVE_RESULT_LOSS_FAULT]
    )
    assert args.fault == NATIVE_RESULT_LOSS_FAULT
