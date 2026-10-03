#!/usr/bin/env python3
"""Select existing resident recovery evaluators; development evidence, no holdouts."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import uuid
from pathlib import Path

from run_hybrid import ROOT, RUNS, _command, _prepare_images, _require

from entryplug_evaluation.harbor.demo import run_resident_demo
from entryplug_evaluation.harbor.faults import score_repair_result_loss
from entryplug_evaluation.harbor.native_result_loss import score_native_result_loss
from entryplug_evaluation.harbor.quiescence_loss import run_quiescence_loss, score_quiescence_loss
from entryplug_evaluation.harbor.recovery import summarize_recovery_episode
from entryplug_evaluation.harbor.repair_cancel import run_repair_cancel, score_repair_cancel
from entryplug_harbor.recovery_qualification import HARBOR_RESIDENT_RECOVERY_V1 as PROFILE
from entryplug_harbor.resident import (
    NATIVE_RESULT_LOSS_FAULT,
    RESULT_LOSS_FAULT,
    ResidentStartupFailure,
)
from entryplug_harbor.runtime import DEFAULT_HARBOR_IMAGE, harbor_container_name

# Fixed development seed and evaluator-only faults. No parameter can select a holdout.
CASES = (
    ("no_fault", None),
    ("recoverable_loss", "kill-active-worker"),
    ("stale_replacement", "kill-active-worker-stale-alternate"),
    ("no_replacement", "kill-both-workers"),
    ("private_result_loss", RESULT_LOSS_FAULT),
    ("native_result_unobserved", NATIVE_RESULT_LOSS_FAULT),
    ("repair_cancel", "repair_cancel"),
    ("quiescence_loss", "quiescence_loss"),
)


def _read(path: Path) -> dict:
    data = json.loads(path.read_text())
    _require(isinstance(data, dict), f"malformed {path.name}")
    return data


def check_repair_identity(evidence: Path) -> dict[str, object]:
    """Join the retained admitted goal to the actual faulted task, with no new budget."""
    summary = _read(evidence / "resident-demo.json")
    operations = summary.get("operation_ids")
    _require(isinstance(operations, list) and len(operations) >= 2, "missing repair operations")
    operation = operations[1]
    _require(
        isinstance(operation, str)
        and len(operation) == 32
        and all(c in "0123456789abcdef" for c in operation),
        "unsafe operation identity",
    )
    goal = _read(evidence / f"goal-{operation}.json")
    task = _read(evidence / "task-0002.json")
    _require(
        goal.get("operation_id") == task.get("operation_id") == operation,
        "repair replaced the admitted operation",
    )
    _require(
        goal.get("runtime_id") == summary.get("runtime_id") and goal.get("run_id") == evidence.name,
        "repair changed the runtime",
    )
    target, actual = goal.get("target_y_px"), task.get("target_y_px")
    deadline, completion = task.get("accepted_deadline_monotonic"), task.get("completed_monotonic")
    _require(
        all(
            type(value) in (int, float) and math.isfinite(value)
            for value in (target, actual, deadline, completion)
        ),
        "missing finite repair timing/target",
    )
    _require(math.isclose(target, actual, abs_tol=1e-6), "repair changed the goal")
    _require(
        deadline == goal.get("deadline_monotonic") and 0 < completion <= deadline,
        "repair changed or exceeded the admitted deadline",
    )
    return {
        "operation_id": operation,
        "runtime_id": goal["runtime_id"],
        "target_y_px": target,
        "deadline_monotonic": deadline,
        "completed_monotonic": completion,
    }


def check_world_removed(evidence: Path) -> None:
    name = harbor_container_name(evidence.name)
    remaining = _command(
        ["docker", "ps", "-a", "--filter", f"name=^/{name}$", "--format", "{{.Names}}"],
        "checking owned ROS world removal",
    )
    _require(not remaining.strip(), "owned ROS world survived Session close")


async def run_case(name: str, fault: str | None, *, seed: int) -> dict[str, object]:
    evidence = None
    try:
        if name == "repair_cancel":
            result = await run_repair_cancel(ROOT, seed=seed, image=DEFAULT_HARBOR_IMAGE)
            evidence = result.evidence_path
            score = score_repair_cancel(evidence, seed=seed)
            passed = result.passed and score.get("safety_outcome") == "passed"
        elif name == "quiescence_loss":
            result = await run_quiescence_loss(ROOT, seed=seed, image=DEFAULT_HARBOR_IMAGE)
            evidence = result.evidence_path
            score = score_quiescence_loss(evidence, seed=seed)
            passed = result.passed and score.get("safety_outcome") == "passed"
        else:
            result = await run_resident_demo(
                ROOT, seed=seed, image=DEFAULT_HARBOR_IMAGE, evaluation_fault=fault
            )
            evidence = result.evidence_path
            if fault == RESULT_LOSS_FAULT:
                score = score_repair_result_loss(evidence, seed=seed)
                passed = not result.passed and score.get("safety_outcome") == "passed"
            elif fault == NATIVE_RESULT_LOSS_FAULT:
                score = score_native_result_loss(evidence, seed=seed)
                passed = not result.passed and score.get("safety_outcome") == "passed"
            else:
                score = summarize_recovery_episode(evidence, seed=seed, fault=fault)
                passed = score.get("outcome") == "passed" and result.passed == (
                    fault in {None, "kill-active-worker"}
                )
        row = {
            "case": name,
            "fault": fault,
            "seed": seed,
            "status": "passed" if passed else "failed",
            "run_id": evidence.name,
            "evidence_path": str(evidence),
            "score": score,
        }
        if name == "recoverable_loss":
            row["admitted_repair"] = check_repair_identity(evidence)
        check_world_removed(evidence)
        row["world_removed"] = True
        return row
    except Exception as error:
        if isinstance(error, ResidentStartupFailure):
            evidence = error.evidence_path
        return {
            "case": name,
            "fault": fault,
            "seed": seed,
            "status": "failed",
            "reason": type(error).__name__,
            "detail": str(error),
            "stop_confirmed": error.stop_confirmed
            if isinstance(error, ResidentStartupFailure)
            else None,
            "evidence_path": str(evidence) if evidence is not None else None,
        }


async def run_cases(run_dir: Path) -> list[dict[str, object]]:
    rows = []
    for name, fault in CASES:
        print(f"ROS recovery development: {name}", file=sys.stderr, flush=True)
        row = await run_case(name, fault, seed=PROFILE.development_seeds[0])
        with (run_dir / f"{name}.json").open("x") as stream:
            json.dump(row, stream, indent=2, allow_nan=False)
            stream.write("\n")
        rows.append(row)
        print(f"ROS recovery development: {name} {row['status']}", file=sys.stderr, flush=True)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--build", action="store_true")
    args = parser.parse_args()
    run_id = "ros2-recovery-" + uuid.uuid4().hex[:12]
    run_dir = RUNS / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    outcome: dict[str, object] = {
        "status": "failed",
        "run_id": run_id,
        "phase": "development",
        "profile_id": PROFILE.profile_id,
        "profile_digest": PROFILE.digest,
        "seed": PROFILE.development_seeds[0],
        "dependencies": [
            "ROS 2 camera and trajectory controller",
            "MuJoCo arm",
            "local detector worker processes",
        ],
        "claim_boundary": (
            "One-dimensional local image-row tasks. No release freeze, "
            "admission-acknowledgement loss, physical hardware or holdout claim."
        ),
    }
    try:
        outcome.update(_prepare_images(build=args.build, mixed=False, native_worker=False))
        rows = asyncio.run(run_cases(run_dir))
        outcome.update(
            cases=rows,
            status="passed"
            if len(rows) == len(CASES) and all(row["status"] == "passed" for row in rows)
            else "failed",
        )
    except Exception as error:
        outcome.update(reason=type(error).__name__, detail=str(error))
    with (run_dir / "scenario.json").open("x") as stream:
        json.dump(outcome, stream, indent=2, allow_nan=False)
        stream.write("\n")
    print(json.dumps(outcome, sort_keys=True))
    return 0 if outcome["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
