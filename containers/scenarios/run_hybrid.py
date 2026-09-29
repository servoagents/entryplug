#!/usr/bin/env python3
"""Own, verify and tear down the disposable hybrid scenario from a checkout."""

from __future__ import annotations

import argparse
import hashlib
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

    def __init__(self, project: str, run_id: str, *, pull_images: bool) -> None:
        self.project = project
        self.pull_images = pull_images
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
        options = [*arguments]
        if options[:1] == ["up"] and not self.pull_images:
            options.extend(("--pull", "never"))
        return _command(
            ["docker", "compose", *files, *options],
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


def _source_digest() -> str:
    tracked = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    ).stdout
    digest = hashlib.sha256()
    for relative in sorted(set(tracked.split(b"\0")) - {b""}):
        path = ROOT / os.fsdecode(relative)
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        if path.is_symlink():
            content = os.fsencode(os.readlink(path))
        elif path.is_file():
            content = path.read_bytes()
        else:
            content = b"<missing>"
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _prepare_images(*, build: bool) -> dict[str, object]:
    source_sha256 = _source_digest()
    images: dict[str, str] = {}
    for dockerfile, tag in (
        ("containers/harbor/Dockerfile", "entryplug-harbor:jazzy"),
        ("containers/mixed/Dockerfile.harbor", "entryplug-harbor-mixed:jazzy"),
        ("containers/scenarios/Dockerfile.worker", "entryplug-scenario-worker:dev"),
    ):
        if build:
            _command(
                [
                    "docker",
                    "build",
                    "--file",
                    dockerfile,
                    "--tag",
                    tag,
                    "--label",
                    f"org.entryplug.source-sha256={source_sha256}",
                    ".",
                ],
                f"building source-current {tag}",
            )
        try:
            image_id = _command(
                ["docker", "image", "inspect", "--format", "{{.Id}}", tag],
                f"checking image identity for {tag}",
            ).strip()
            recorded_source = _command(
                [
                    "docker",
                    "image",
                    "inspect",
                    "--format",
                    '{{index .Config.Labels "org.entryplug.source-sha256"}}',
                    tag,
                ],
                f"checking source identity for {tag}",
            ).strip()
        except ScenarioFailure as error:
            raise ScenarioFailure(f"{tag} is missing; rerun with --build") from error
        if recorded_source != source_sha256:
            raise ScenarioFailure(f"{tag} is stale; rerun with --build")
        images[tag] = image_id
    return {"source_sha256": source_sha256, "image_ids": images}


def _check_secret(run_dir: Path, token_file: Path) -> None:
    secret = token_file.read_bytes().strip()
    if not secret:
        raise ScenarioFailure("fixture token is empty")
    for artifact in run_dir.iterdir():
        if artifact.is_file() and secret in artifact.read_bytes():
            raise ScenarioFailure("fixture token leaked into task evidence")


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
    _check_secret(run_dir, token_file)
    return {
        "status": "passed",
        "operation_ids": [operation["operation_id"] for operation in operations],
        "lighting_writes": [operation["result"]["lighting_writes"] for operation in operations],
        "bridge_applied_count": bridge["applied_count"],
        "home_assistant_entity": report["home_assistant_entity"],
    }


def _check_unavailable_result(run_dir: Path, token_file: Path) -> dict[str, object]:
    report = json.loads((run_dir / "hybrid.json").read_text(encoding="utf-8"))
    bridge = json.loads((run_dir / "bridge.json").read_text(encoding="utf-8"))
    operations = report.get("operations")
    if report.get("status") != "failed" or not isinstance(operations, list) or len(operations) != 1:
        raise ScenarioFailure("missing-worker task did not return one failed operation")
    operation = operations[0]
    result = operation.get("result")
    if (
        operation.get("lifecycle") != "failed"
        or operation.get("reason_code") != "OBSERVATION_PROVIDER_FAILED"
        or not isinstance(result, dict)
        or result.get("lighting_writes") != 0
        or bridge.get("status") != "stopped"
        or bridge.get("applied_count") != 0
    ):
        raise ScenarioFailure("missing-worker task made a write or returned the wrong refusal")
    _check_secret(run_dir, token_file)
    return {
        "status": "passed",
        "task_lifecycle": "failed",
        "task_reason": operation["reason_code"],
        "operation_ids": [operation["operation_id"]],
        "lighting_writes": [0],
        "bridge_applied_count": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--build", action="store_true", help="build source-current images")
    parser.add_argument("--fault", choices=("none", "no-native-worker"), default="none")
    args = parser.parse_args()
    RUNS.mkdir(exist_ok=True)
    run_id = "hybrid-" + uuid.uuid4().hex[:12]
    run_dir = RUNS / run_id
    bootstraps: list[dict[str, object]] = []
    outcome: dict[str, object] = {
        "status": "failed",
        "run_id": run_id,
        "fault": args.fault,
        "fresh_volume_bootstraps": bootstraps,
    }
    stack: OwnedStack | None = None
    try:
        outcome.update(_prepare_images(build=args.build))
        for project in (f"ep-{run_id}-proof", f"ep-{run_id}"):
            stack = OwnedStack(project, run_id, pull_images=args.build)
            try:
                bootstraps.append(
                    {
                        "fixture": "proof" if project.endswith("-proof") else "task",
                        **stack.bootstrap(),
                    }
                )
                if project.endswith("-proof"):
                    continue
                services = ["native-router", "workbench"]
                if args.fault == "none":
                    services.insert(1, "native-worker")
                stack.compose(
                    "up",
                    "-d",
                    "--no-build",
                    *services,
                    hybrid=True,
                    label="starting native compute and ROS workbench",
                )
                command_failed = False
                try:
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
                        label="running inspect_target task",
                    )
                except ScenarioFailure:
                    if args.fault != "no-native-worker":
                        raise
                    command_failed = True
                if args.fault == "no-native-worker":
                    if not command_failed:
                        raise ScenarioFailure("task unexpectedly succeeded without native worker")
                    outcome.update(_check_unavailable_result(run_dir, stack.private / "ha-token"))
                else:
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
