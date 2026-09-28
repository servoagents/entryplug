from __future__ import annotations

import json
from pathlib import Path

import pytest

from entryplug.cli import _parse_args
from entryplug_evaluation.harbor.repair_cancel import score_repair_cancel
from entryplug_harbor.recovery_qualification import HARBOR_RESIDENT_RECOVERY_V1 as PROFILE
from entryplug_harbor.resident import SOURCE_ID, SOURCE_LINEAGE


def _write(path: Path, value: dict[str, object]) -> None:
    path.write_text(json.dumps(value) + "\n", encoding="utf-8")


def _episode(path: Path) -> None:
    path.mkdir()
    _write(
        path / "resident-repair-cancel.json",
        {
            "experiment_seed": 101,
            "target_offsets_px": list(PROFILE.target_offsets(101)),
            "qualification_profile": {"digest": PROFILE.digest},
            "evaluation_fault_profile": "kill-active-worker",
            "world_count": 1,
            "world_stop_confirmed": True,
            "marker_observed_before_cancel": True,
            "cancel_requested_monotonic": 12.0,
            "operations": [
                {"operation_id": "first", "lifecycle": "succeeded"},
                {
                    "operation_id": "second",
                    "lifecycle": "canceled",
                    "motion_state": "holding",
                    "cancel_requested": True,
                    "reason_code": "CANCEL_REQUESTED",
                    "quiescence_confirmed": True,
                },
            ],
        },
    )
    _write(
        path / "task-0002-repair-started.evaluator.json",
        {
            "operation_id": "second",
            "fault_phase": PROFILE.fault_phase,
            "detected_monotonic": 10.0,
        },
    )
    _write(
        path / "task-0002-repair-goal-accepted.evaluator.json",
        {"operation_id": "second", "goal_id": "repair-goal", "accepted_monotonic": 11.0},
    )
    _write(path / "task-0001.json", {"operation_id": "first", "status": "passed"})
    _write(
        path / "task-0002.json",
        {
            "operation_id": "second",
            "source_id": SOURCE_ID,
            "lineage_id": SOURCE_LINEAGE,
            "status": "canceled",
            "reason_code": "CANCEL_REQUESTED",
            "quiescence_confirmed": True,
            "binding_ready": False,
            "fault_evaluator_only": {"applied": True, "phase": PROFILE.fault_phase},
            "recovery": {"status": "loss_detected"},
            "evaluator_only": {"false_visual_completion": False},
        },
    )
    _write(
        path / "task-0002-trace.jsonl",
        {"purpose": f"task-0002:servo-step-{PROFILE.fault_servo_step}"},
    )
    with (path / "task-0002-trace.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "purpose": "task-0002:replacement-check-1",
                    "goal_id": "repair-goal",
                    "native_status": 5,
                    "cancel_requested": True,
                    "cancel_acknowledged": True,
                    "stop_confirmed_from_feedback": True,
                }
            )
            + "\n"
        )


def test_cancel_score_requires_detected_loss_and_confirmed_hold(tmp_path: Path) -> None:
    run = tmp_path / "cancel"
    _episode(run)
    assert score_repair_cancel(run, seed=101)["safety_outcome"] == "passed"


@pytest.mark.parametrize(
    "violation",
    ("early_cancel", "resumed", "missing_ack", "unknown_stop", "third_task", "wrong_profile"),
)
def test_cancel_score_rejects_unsafe_or_misidentified_evidence(
    tmp_path: Path, violation: str
) -> None:
    run = tmp_path / "cancel"
    _episode(run)
    summary_path = run / "resident-repair-cancel.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if violation == "early_cancel":
        summary["cancel_requested_monotonic"] = 9.0
    elif violation == "resumed":
        with (run / "task-0002-trace.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"purpose": "task-0002:resumed:servo-step-1"}) + "\n")
    elif violation == "missing_ack":
        artifact = run / "task-0002-trace.jsonl"
        actions = [json.loads(line) for line in artifact.read_text(encoding="utf-8").splitlines()]
        actions[1]["cancel_acknowledged"] = False
        artifact.write_text(
            "".join(json.dumps(action) + "\n" for action in actions), encoding="utf-8"
        )
    elif violation == "unknown_stop":
        summary["operations"][1]["quiescence_confirmed"] = False
    elif violation == "third_task":
        _write(run / "task-0003.json", {})
    else:
        summary["qualification_profile"]["digest"] = "wrong"
    _write(summary_path, summary)
    assert score_repair_cancel(run, seed=101)["safety_outcome"] == "failed"


def test_cancel_case_is_private_and_development_only(tmp_path: Path) -> None:
    args = _parse_args(["resident-repair-cancel", "--runtime", "container", "--seed", "202"])
    assert args.seed == 202
    with pytest.raises(ValueError, match="development seeds only"):
        score_repair_cancel(tmp_path, seed=PROFILE.holdout_seeds[0])
