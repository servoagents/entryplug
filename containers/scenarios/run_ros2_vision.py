#!/usr/bin/env python3
"""Late ROS world connection, continuing visual goals, and existing Mirrors checks."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import sys
import time
import uuid
from pathlib import Path

from run_hybrid import ROOT, RUNS, _prepare_images, _require
from run_ros2_recovery import check_world_removed

from entryplug.core.evidence import json_object
from entryplug.core.operation import AdmissionError, Lifecycle
from entryplug_evaluation.harbor.pilot import summarize_mirrors_episode
from entryplug_harbor.qualification import HARBOR_MIRRORS_V1
from entryplug_harbor.recovery_qualification import HARBOR_RESIDENT_RECOVERY_V1 as PROFILE
from entryplug_harbor.resident import (
    ResidentSession,
    harbor_resident_episode_factory,
    start_owned_resident,
)
from entryplug_harbor.runtime import DEFAULT_HARBOR_IMAGE, run_harbor
from entryplug_harbor.visual_task import VISUAL_REACH


def check_session(report: dict, evidence: Path) -> None:
    before, joined, lost = (
        report.get(k, {}) for k in ("before_connect", "after_connect", "after_loss")
    )
    runtime = report.get("runtime_id")
    _require(
        isinstance(runtime, str)
        and bool(runtime)
        and all(v.get("runtime_id") == runtime for v in (before, joined, lost)),
        "ROS connection restarted the runtime",
    )
    _require(
        before.get("available") is False
        and before.get("run_id") is None
        and report.get("initial_refusal") == "WORLD_UNAVAILABLE"
        and report.get("initial_operations") == 0,
        "missing ROS resources did not refuse before dispatch",
    )
    _require(
        joined.get("available") is True
        and joined.get("run_id") == evidence.name
        and joined.get("readiness_basis") == "native_acquisition_and_checked_binding",
        "ROS world did not supply checked readiness",
    )
    _require(
        lost.get("available") is False and report.get("post_loss_refusal") == "WORLD_UNAVAILABLE",
        "lost ROS world did not fence further admission",
    )
    absent_at, started_at = (
        report.get("missing_observed_monotonic"),
        report.get("world_start_requested_monotonic"),
    )
    _require(
        all(
            type(value) in (int, float) and math.isfinite(value)
            for value in (absent_at, started_at)
        )
        and 0 < absent_at <= started_at,
        "missing endpoint report did not precede startup",
    )
    operations = report.get("operations")
    _require(
        isinstance(operations, list)
        and len(operations) == 3
        and all(
            isinstance(op, dict)
            and isinstance(op.get("operation_id"), str)
            and len(op["operation_id"]) == 32
            and all(c in "0123456789abcdef" for c in op["operation_id"])
            for op in operations
        )
        and len({op.get("operation_id") for op in operations}) == 3,
        "missing distinct continuing operations",
    )
    _require(
        operations[2].get("lifecycle") == "failed"
        and operations[2].get("reason_code") == "REPLY_LOST_STOPPED"
        and operations[2].get("result", {}).get("quiescence_confirmed") is True,
        "resource loss produced false completion or unknown stop",
    )
    acquisition = json.loads((evidence / "resident-acquisition.json").read_text())
    _require(
        acquisition.get("status") == "passed"
        and acquisition.get("validation", {}).get("status") == "passed"
        and acquisition.get("association", {}).get("status") == "selected"
        and isinstance(acquisition.get("binding", {}).get("key"), str)
        and bool(acquisition["binding"]["key"]),
        "native acquisition did not qualify",
    )
    previous_sequence = -1
    for index, op in enumerate(operations[:2], 1):
        result = op.get("result", {})
        _require(
            op.get("lifecycle") == "succeeded"
            and op.get("motion_state") == "holding"
            and result.get("run_id") == evidence.name
            and result.get("source_id") == "camera-color-v1"
            and result.get("lineage_id") == "camera-color-capture-v1"
            and result.get("binding_evidence_key") == acquisition.get("binding", {}).get("key")
            and result.get("quiescence_confirmed") is True
            and result.get("binding_revision") == 1
            and result.get("validation", {}).get("status") == "reused",
            "visual goal lacks the acquired local binding",
        )
        task = json.loads((evidence / f"task-{index:04d}.json").read_text())
        goal = json.loads((evidence / f"goal-{op['operation_id']}.json").read_text())
        _require(
            task.get("operation_id") == op["operation_id"]
            and goal.get("runtime_id") == runtime
            and math.isclose(task["target_y_px"], goal["target_y_px"], abs_tol=1e-6),
            "goal identity changed",
        )
        score = task.get("evaluator_only", {})
        _require(
            score.get("independently_inside_tolerance") is True
            and score.get("false_visual_completion") is False,
            "raw image does not independently confirm visual completion",
        )
        sequence = result.get("detector_worker", {}).get("frame_sequence")
        _require(
            type(sequence) is int and sequence > previous_sequence,
            "warm goal reused old observation",
        )
        previous_sequence = sequence
        for suffix in ("before.raw.png", "after.raw.png"):
            path = evidence / f"task-{index:04d}-{suffix}"
            _require(
                path.is_file()
                and not path.is_symlink()
                and path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
                and hashlib.sha256(path.read_bytes()).hexdigest()
                == task.get("raw_artifacts", {}).get(path.name),
                "missing raw visual evidence",
            )
    _require(
        report.get("world_stop_confirmed") is True and report.get("world_removed") is True,
        "ROS owner cleanup not confirmed",
    )


async def run_connection(run_dir: Path) -> dict:
    report: dict = {"status": "failed", "operations": []}
    owned = []
    session = None
    evidence = None

    async def before_connect(client: ResidentSession) -> None:
        nonlocal session
        session = client
        report["before_connect"] = json_object(await client.inspect("world"), "before connect")
        report["runtime_id"] = (await client.observe()).runtime_id
        report["missing_observed_monotonic"] = time.monotonic()
        try:
            await client.act(VISUAL_REACH, {"target_y_px": 240}, request_id="missing-world")
        except AdmissionError as error:
            report["initial_refusal"] = error.reason_code
        report["initial_operations"] = len((await client.observe()).operations)
        _require(
            report.get("initial_refusal") == "WORLD_UNAVAILABLE" and not owned,
            "world started before missing-resource report",
        )
        with (run_dir / "before-connect.json").open("x") as stream:
            json.dump(report, stream, indent=2)

    async def start(*args):
        report["world_start_requested_monotonic"] = time.monotonic()
        run = await start_owned_resident(*args)
        owned.append(run)
        return run

    try:
        episode = await harbor_resident_episode_factory(
            ROOT, before_connect=before_connect, start_resident=start
        )(PROFILE.development_seeds[0])
        _require(episode.session is session, "connection replaced the Session")
        evidence = owned[0].evidence_path
        report["evidence_path"] = str(evidence)
        report["after_connect"] = json_object(await session.inspect("world"), "after connect")
        initial = episode.public_metadata["initial_y_px"]
        for index, offset in enumerate(PROFILE.target_offsets(101)[:2]):
            op = await session.act(
                VISUAL_REACH, {"target_y_px": initial + offset}, request_id=f"joined-{index}"
            )
            terminal = await session.wait(op, PROFILE.operation_deadline_seconds + 2)
            report["operations"].append(
                json_object(await session.inspect(op, "result"), "joined goal")
            )
            _require(
                terminal.lifecycle == Lifecycle.SUCCEEDED, "joined ROS visual goal did not succeed"
            )
        # Evaluator removes only the now-idle owned world, never task authority.
        stopped = await owned[0].stop()
        _require(stopped.confirmed, "owned world stop failed")
        op = await session.act(VISUAL_REACH, {"target_y_px": initial}, request_id="lost-world")
        await session.wait(op, PROFILE.operation_deadline_seconds + 2)
        report["operations"].append(json_object(await session.inspect(op, "result"), "lost goal"))
        report["after_loss"] = json_object(await session.inspect("world"), "after loss")
        try:
            await session.act(VISUAL_REACH, {"target_y_px": initial}, request_id="fenced-world")
        except AdmissionError as error:
            report["post_loss_refusal"] = error.reason_code
    except Exception as error:
        report.update(reason=type(error).__name__, detail=str(error))
    finally:
        if session is not None:
            try:
                await session.close()
                report["world_stop_confirmed"] = True
            except Exception as error:
                report.update(
                    reason=type(error).__name__, detail=str(error), world_stop_confirmed=False
                )
    if evidence is not None:
        try:
            check_world_removed(evidence)
            report["world_removed"] = True
            check_session(report, evidence)
            report["raw_artifacts"] = [
                {
                    "path": str(p.relative_to(ROOT)),
                    "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                }
                for p in sorted(evidence.glob("task-*.raw.png"))
            ]
            report["status"] = "passed"
        except Exception as error:
            report.update(reason=type(error).__name__, detail=str(error))
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--build", action="store_true")
    args = parser.parse_args()
    run_id = "ros2-vision-" + uuid.uuid4().hex[:12]
    run_dir = RUNS / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    outcome: dict = {
        "status": "failed",
        "run_id": run_id,
        "phase": "development",
        "dependencies": [
            "ROS 2 camera and trajectory controller",
            "MuJoCo arm",
            "local detector processes",
        ],
        "claim_boundary": (
            "Local one-dimensional image-row alignment. Mirrors uses a separate "
            "owned world; no 3D or hardware claim."
        ),
    }
    try:
        outcome.update(_prepare_images(build=args.build, mixed=False, native_worker=False))
        print(
            "ROS vision: missing endpoints, late connection, warm task and world loss",
            file=sys.stderr,
            flush=True,
        )
        connection = asyncio.run(run_connection(run_dir))
        outcome["connection"] = connection
        with (run_dir / "connection.json").open("x") as stream:
            json.dump(connection, stream, indent=2, allow_nan=False)
        print("ROS vision: existing Hall of Mirrors development case", file=sys.stderr, flush=True)
        result = run_harbor(
            ROOT,
            image=DEFAULT_HARBOR_IMAGE,
            case="mirrors",
            seed=HARBOR_MIRRORS_V1.development_seeds[0],
            capture_output=True,
        )
        mirrors = summarize_mirrors_episode(101, result)
        check_world_removed(result.evidence_path)
        outcome["mirrors"] = mirrors
        outcome["status"] = (
            "passed"
            if connection["status"] == "passed" and mirrors["outcome"] == "passed"
            else "failed"
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
