#!/usr/bin/env python3
"""Own, verify and tear down the disposable hybrid scenario from a checkout."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "containers/scenarios/compose.yaml"
HYBRID = ROOT / "containers/scenarios/compose.hybrid.yaml"
RUNS = ROOT / "runs"


class ScenarioFailure(RuntimeError):
    pass


def _command(args: list[str], label: str, *, environment: dict[str, str] | None = None) -> str:
    print(label, file=sys.stderr, flush=True)
    result = subprocess.run(
        args,
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    if result.returncode != 0:
        if label == "enrolling fresh HA fixture":
            try:
                failure = json.loads(result.stdout.strip())
                stage = failure.get("stage")
            except (json.JSONDecodeError, AttributeError):
                stage = None
            if isinstance(stage, str):
                raise ScenarioFailure(f"{label}: {stage}")
        raise ScenarioFailure(f"{label} returned status {result.returncode}")
    return result.stdout


class OwnedStack:
    """One project ID, HA volume and private secret directory; no global cleanup."""

    def __init__(self, project: str, run_id: str) -> None:
        self.project = project
        self.private = Path(tempfile.mkdtemp(prefix="entryplug-scenario-"))
        self.environment = dict(os.environ)
        self.environment.update(
            ENTRYPLUG_COMPOSE_PROJECT=project,
            ENTRYPLUG_FIXTURE_PRIVATE=str(self.private),
            ENTRYPLUG_SCENARIO_RUN_ID=run_id,
            ENTRYPLUG_RUNS_DIR=str(RUNS),
        )

    def compose(self, *arguments: str, hybrid: bool = False, label: str) -> str:
        files = ["-f", str(COMPOSE)]
        if hybrid:
            files += ["-f", str(HYBRID)]
        return _command(
            ["docker", "compose", *files, *arguments],
            label,
            environment=self.environment,
        )

    def bootstrap(self) -> dict[str, object]:
        self.compose("up", "-d", "--no-build", label="starting private HA/MQTT fixture")
        output = self.compose(
            "exec",
            "-T",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "homeassistant",
            "python3",
            "/opt/entryplug/ha_bootstrap.py",
            label="enrolling fresh HA fixture",
        )
        try:
            report = json.loads(output.strip())
        except json.JSONDecodeError as error:
            raise ScenarioFailure("HA bootstrap did not return scrubbed JSON") from error
        if report.get("status") != "passed":
            raise ScenarioFailure("fresh HA bootstrap did not pass")
        token = self.private / "ha-token"
        if token.stat().st_mode & 0o777 != 0o600:
            raise ScenarioFailure("fixture token permissions are not 0600")
        return report

    def close(self) -> None:
        self.compose(
            "down", "-v", "--remove-orphans", hybrid=True, label="tearing down owned fixture"
        )
        if self.private.parent != Path(tempfile.gettempdir()) or not self.private.name.startswith(
            "entryplug-scenario-"
        ):
            raise ScenarioFailure("private fixture path was not a generated temporary directory")
        shutil.rmtree(self.private)


def _build_images() -> None:
    for dockerfile, tag in (
        ("containers/harbor/Dockerfile", "entryplug-harbor:jazzy"),
        ("containers/mixed/Dockerfile.harbor", "entryplug-harbor-mixed:jazzy"),
        ("containers/scenarios/Dockerfile.worker", "entryplug-scenario-worker:dev"),
    ):
        _command(
            ["docker", "build", "--file", dockerfile, "--tag", tag, "."],
            f"building source-current {tag}",
        )


def _check_result(run_dir: Path, token_file: Path) -> dict[str, object]:
    report = json.loads((run_dir / "hybrid.json").read_text(encoding="utf-8"))
    bridge = json.loads((run_dir / "bridge.json").read_text(encoding="utf-8"))
    if report.get("status") != "passed" or bridge.get("status") != "stopped":
        raise ScenarioFailure("hybrid task or world-owned bridge did not pass")
    operations = report.get("operations")
    if not isinstance(operations, list) or len(operations) != 2:
        raise ScenarioFailure("hybrid task did not return two operations")
    first = operations[0].get("result")
    if not isinstance(first, dict) or bridge.get("applied_count") != first.get("lighting_writes"):
        raise ScenarioFailure("MQTT bridge did not confirm every task lighting write")
    secret = token_file.read_bytes().strip()
    if not secret:
        raise ScenarioFailure("fixture token is empty")
    for artifact in run_dir.iterdir():
        if artifact.is_file() and secret in artifact.read_bytes():
            raise ScenarioFailure("fixture token leaked into task evidence")
    return {
        "status": "passed",
        "operation_ids": [operation["operation_id"] for operation in operations],
        "lighting_writes": [operation["result"]["lighting_writes"] for operation in operations],
        "bridge_applied_count": bridge["applied_count"],
        "home_assistant_entity": report["home_assistant_entity"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--build",
        action="store_true",
        help="required for this first slice so local images match the source tree",
    )
    args = parser.parse_args()
    if not args.build:
        parser.error("slice A requires --build; cached layers make unchanged reruns cheap")
    RUNS.mkdir(exist_ok=True)
    run_id = "hybrid-" + uuid.uuid4().hex[:12]
    run_dir = RUNS / run_id
    bootstraps: list[dict[str, object]] = []
    outcome: dict[str, object] = {
        "status": "failed",
        "run_id": run_id,
        "fresh_volume_bootstraps": bootstraps,
    }
    stack: OwnedStack | None = None
    try:
        _build_images()
        for project in (f"ep-{run_id}-proof", f"ep-{run_id}"):
            stack = OwnedStack(project, run_id)
            try:
                bootstraps.append(
                    {
                        "fixture": "proof" if project.endswith("-proof") else "task",
                        **stack.bootstrap(),
                    }
                )
                if project.endswith("-proof"):
                    continue
                stack.compose(
                    "up",
                    "-d",
                    "--no-build",
                    "native-router",
                    "native-worker",
                    "workbench",
                    hybrid=True,
                    label="starting native compute and ROS workbench",
                )
                stack.compose(
                    "exec",
                    "-T",
                    "workbench",
                    "/usr/local/bin/entryplug-container",
                    "bash",
                    "/workspace/entryplug/containers/mixed/hybrid_inspection.sh",
                    f"/workspace/entryplug/runs/{run_id}",
                    run_id,
                    hybrid=True,
                    label="running first and warm inspect_target operations",
                )
                outcome.update(_check_result(run_dir, stack.private / "ha-token"))
            finally:
                stack.close()
                stack = None
    except Exception as error:
        outcome["status"] = "failed"
        outcome["reason"] = type(error).__name__
        print(f"hybrid scenario failed: {error}", file=sys.stderr)
        if stack is not None:
            print(
                f"owned fixture may remain: {stack.project}; private data: {stack.private}",
                file=sys.stderr,
            )
    finally:
        run_dir.mkdir(exist_ok=True)
        with (run_dir / "scenario.json").open("x", encoding="utf-8") as stream:
            json.dump(outcome, stream, indent=2, sort_keys=True)
            stream.write("\n")
    print(json.dumps(outcome, sort_keys=True))
    return 0 if outcome["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
