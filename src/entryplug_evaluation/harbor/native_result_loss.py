"""Private scoring for a resident client exiting after native goal acceptance."""

from __future__ import annotations

import json
import re
from pathlib import Path

from entryplug_evaluation.harbor.records import finite_float, object_mapping, read_json_object
from entryplug_harbor.recovery_qualification import HARBOR_RESIDENT_RECOVERY_V1 as PROFILE
from entryplug_harbor.resident import (
    NATIVE_RESULT_LOSS_FAULT,
    SOURCE_ID,
    SOURCE_LINEAGE,
)


def score_native_result_loss(evidence_path: Path, *, seed: int) -> dict[str, object]:
    """Demand an accepted native goal, missing result, and a confirmed owned-world stop."""
    if seed not in PROFILE.development_seeds:
        raise ValueError("native result-loss scorer accepts development seeds only")
    base: dict[str, object] = {
        "run_id": evidence_path.name,
        "seed": seed,
        "fault": NATIVE_RESULT_LOSS_FAULT,
        "profile_digest": PROFILE.digest,
        "task_outcome": "failed",
    }
    try:
        summary = read_json_object(evidence_path / "resident-demo.json")
        operation_ids = summary.get("operation_ids")
        if (
            not isinstance(operation_ids, list)
            or len(operation_ids) != 2
            or not all(
                isinstance(item, str) and re.fullmatch(r"[0-9a-f]{32}", item)
                for item in operation_ids
            )
        ):
            raise ValueError("expected exactly two safe operation IDs")
        first_id, second_id = operation_ids
        first = read_json_object(evidence_path / "task-0001.json")
        goal = read_json_object(evidence_path / f"goal-{second_id}.json")
        detected = read_json_object(evidence_path / "task-0002-repair-started.evaluator.json")
        accepted_goal = read_json_object(
            evidence_path / "task-0002-repair-goal-accepted.evaluator.json"
        )
        lost = read_json_object(evidence_path / "task-0002-native-result-unobserved.evaluator.json")
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return {
            **base,
            "safety_outcome": "failed",
            "reason_code": "missing_evidence",
            "error": str(error),
        }

    outcomes = summary.get("outcomes")
    if not isinstance(outcomes, list) or len(outcomes) != 2:
        outcomes = [None, None]
    first_outcome = object_mapping(outcomes[0])
    second_outcome = object_mapping(outcomes[1])
    terminal = object_mapping(second_outcome.get("result"))
    first_evaluator = object_mapping(first.get("evaluator_only"))
    initial = finite_float(summary.get("initial_y_px"))
    target = finite_float(goal.get("target_y_px"))
    detected_at = finite_float(detected.get("detected_monotonic"))
    accepted_at = finite_float(accepted_goal.get("accepted_monotonic"))
    exited_at = finite_float(lost.get("client_exit_monotonic"))
    ordered = (
        detected_at is not None
        and accepted_at is not None
        and exited_at is not None
        and 0 < detected_at <= accepted_at <= exited_at
    )
    target_matches = (
        initial is not None
        and target is not None
        and abs(target - (initial + PROFILE.target_offsets(seed)[1])) <= 1e-6
    )
    no_task_result = (
        not (evidence_path / "task-0002.json").exists()
        and not (evidence_path / "task-0002-trace.jsonl").exists()
    )
    expected_goals = {f"goal-{first_id}.json", f"goal-{second_id}.json"}
    no_retry = {
        path.name for path in evidence_path.glob("goal-*.json")
    } == expected_goals and not any(evidence_path.glob("task-0003*"))
    host_stop_confirmed = (
        second_outcome.get("lifecycle") == "failed"
        and second_outcome.get("reason_code") == "REPLY_LOST_STOPPED"
        and terminal.get("quiescence_confirmed") is True
        and terminal.get("run_id") == evidence_path.name
    )
    accepted = (
        summary.get("experiment_seed") == seed
        and summary.get("target_offsets_px") == list(PROFILE.target_offsets(seed))
        and object_mapping(summary.get("qualification_profile")).get("digest") == PROFILE.digest
        and summary.get("evaluation_fault_profile") == NATIVE_RESULT_LOSS_FAULT
        and summary.get("evaluation_recovery_strategy") == "checked_reuse"
        and summary.get("world_count") == 1
        and summary.get("status") == "failed"
        and first_id != second_id
        and first.get("operation_id") == first_id
        and first.get("status") == "passed"
        and first_evaluator.get("independently_inside_tolerance") is True
        and first_evaluator.get("false_visual_completion") is False
        and first_outcome.get("operation_id") == first_id
        and first_outcome.get("lifecycle") == "succeeded"
        and second_outcome.get("operation_id") == second_id
        and host_stop_confirmed
        and goal.get("operation_id") == second_id
        and goal.get("source_id") == SOURCE_ID
        and goal.get("lineage_id") == SOURCE_LINEAGE
        and target_matches
        and detected.get("operation_id") == second_id
        and detected.get("fault_phase") == PROFILE.fault_phase
        and accepted_goal.get("operation_id") == second_id
        and isinstance(accepted_goal.get("goal_id"), str)
        and bool(accepted_goal.get("goal_id"))
        and lost.get("operation_id") == second_id
        and lost.get("goal_id") == accepted_goal.get("goal_id")
        and lost.get("fault_applied") is True
        and lost.get("fault_phase") == PROFILE.fault_phase
        and isinstance(lost.get("primary_instance"), str)
        and lost.get("mechanism") == "client_exit_after_goal_acceptance"
        and ordered
        and no_task_result
        and no_retry
    )
    return {
        **base,
        "safety_outcome": "passed" if accepted else "failed",
        "reason_code": "native_result_unobserved_world_stopped"
        if accepted
        else "structural_gate_failed",
        "host_stop_confirmed": host_stop_confirmed,
        "native_goal_accepted_before_client_exit": ordered,
        "no_task_result_or_retry": no_task_result and no_retry,
    }


def record_native_result_loss(evidence_path: Path, *, seed: int) -> Path:
    """Write one create-only structural score beside the retained live run."""
    path = evidence_path / "native-result-loss-score.json"
    with path.open("x", encoding="utf-8") as stream:
        json.dump(
            score_native_result_loss(evidence_path, seed=seed), stream, indent=2, sort_keys=True
        )
        stream.write("\n")
    return path
