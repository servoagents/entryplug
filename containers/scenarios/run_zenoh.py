#!/usr/bin/env python3
"""Own the native-worker late-join scenario; no HA or MQTT service is started."""

from __future__ import annotations

import argparse
import json
import time
import uuid
from pathlib import Path

from run_hybrid import (
    ROOT,
    RUNS,
    OwnedStack,
    ScenarioFailure,
    _check_inspection_evidence,
    _command,
    _prepare_images,
    _require,
    _stop_native_worker,
)


class NativeStack(OwnedStack):
    def compose(self, *arguments: str, hybrid: bool = False, label: str) -> str:
        options = list(arguments)
        if options[:1] == ["up"]:
            options.extend(("--pull", "never"))
        return _command(
            [
                "docker",
                "compose",
                "-f",
                str(ROOT / "containers/scenarios/compose.zenoh.yaml"),
                *options,
            ],
            label,
            environment=self.environment,
        )


def check_result(run_dir: Path, run_id: str) -> dict[str, object]:
    report = json.loads((run_dir / "zenoh.json").read_text())
    _require(
        report.get("status") == "completed" and report.get("runtime_id") == run_id,
        "native Session did not complete",
    )
    operations = report.get("operations")
    _require(
        isinstance(operations, list) and len(operations) == 4,
        "missing late-join operation evidence",
    )
    _require(all(isinstance(op, dict) for op in operations), "malformed operation")
    _require(
        len({op.get("operation_id") for op in operations}) == 4,
        "late join reused a terminal operation",
    )
    for index in (0, 3):
        op = operations[index]
        _require(
            op.get("lifecycle") == "failed"
            and op.get("reason_code") == "OBSERVATION_PROVIDER_FAILED"
            and op.get("effect_state") == "none"
            and op.get("result", {}).get("lighting_writes") == 0,
            "absent or wrong-generation worker was not refused",
        )
    for op in operations[1:3]:
        _require(
            op.get("lifecycle") == "succeeded"
            and op.get("result", {}).get("worker_id") == "worker-a"
            and op.get("result", {}).get("lighting_writes") == 0,
            "joined worker did not supply read-only inspection",
        )
    _check_inspection_evidence(run_dir, {**report, "operations": operations[1:3]}, profile="zenoh")
    join = json.loads((run_dir / "join-requested.json").read_text())
    _require(
        join.get("runtime_id") == run_id and join.get("operation") == operations[0],
        "endpoint absence was not recorded before joining",
    )
    cleanup = json.loads((run_dir / "world-cleanup.json").read_text())
    _require(
        cleanup.get("status") == "stopped" and cleanup.get("forced_stops") == 0,
        "world did not stop cleanly",
    )
    _require(
        report.get("reconfiguration_generation") == 3,
        "worker changes bypassed the idle reconfiguration barrier",
    )
    return {
        "status": "passed",
        "runtime_id": run_id,
        "operation_ids": [op["operation_id"] for op in operations],
        "task_outcomes": [op["lifecycle"] for op in operations],
        "fixture_lighting_writes": 1,
        "task_lighting_writes": 0,
        "dependencies": ["ROS 2 camera", "MuJoCo panel", "native Zenoh router", "native detector"],
    }


def _wait_file(path: Path, deadline: float) -> None:
    while not path.is_file():
        if time.monotonic() >= deadline:
            raise ScenarioFailure(f"timed out waiting for {path.name}")
        time.sleep(0.1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--build", action="store_true")
    args = parser.parse_args()
    run_id = "zenoh-" + uuid.uuid4().hex[:12]
    run_dir = RUNS / run_id
    RUNS.mkdir(exist_ok=True)
    outcome: dict[str, object] = {"status": "failed", "run_id": run_id}
    stack = None
    try:
        outcome.update(_prepare_images(build=args.build))
        stack = NativeStack(f"ep-{run_id}", run_id, pull_images=False)
        stack.compose(
            "up",
            "-d",
            "--no-build",
            "native-router",
            "workbench",
            label="starting camera world without a detector",
        )
        stack.compose(
            "exec",
            "-T",
            "-d",
            "workbench",
            "/usr/local/bin/entryplug-container",
            "bash",
            "/workspace/entryplug/containers/mixed/hybrid_inspection.sh",
            f"/workspace/entryplug/runs/{run_id}",
            run_id,
            "zenoh",
            label="starting continuing native Session",
        )
        deadline = time.monotonic() + 90
        _wait_file(run_dir / "join-requested.json", deadline)
        before = json.loads((run_dir / "join-requested.json").read_text())["operation"]
        _require(
            before.get("lifecycle") == "failed"
            and before.get("reason_code") == "OBSERVATION_PROVIDER_FAILED",
            "missing-worker operation did not refuse before join",
        )
        stack.compose(
            "up", "-d", "--no-build", "native-worker", label="joining approved native worker"
        )
        while True:
            logs = stack.compose(
                "logs", "--no-color", "native-worker", label="checking worker readiness"
            )
            if "native detector worker-a ready" in logs:
                break
            if time.monotonic() >= deadline:
                raise ScenarioFailure("native worker did not become ready")
            time.sleep(0.25)
        with (run_dir / "worker-joined.json").open("x") as stream:
            json.dump({"worker_id": "worker-a", "generation": 1}, stream)
        _wait_file(run_dir / "zenoh.json", deadline)
        _wait_file(run_dir / "world-cleanup.json", deadline)
        _stop_native_worker(stack, run_dir)
        outcome.update(check_result(run_dir, run_id))
    except Exception as error:
        outcome.update(status="failed", reason=type(error).__name__, detail=str(error))
    finally:
        if stack is not None:
            try:
                stack.close()
            except Exception as error:
                outcome.update(
                    status="failed", cleanup="failed", cleanup_reason=type(error).__name__
                )
        run_dir.mkdir(exist_ok=True)
        with (run_dir / "scenario.json").open("x") as stream:
            json.dump(outcome, stream, indent=2, allow_nan=False)
            stream.write("\n")
    print(json.dumps(outcome, sort_keys=True))
    return 0 if outcome["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
