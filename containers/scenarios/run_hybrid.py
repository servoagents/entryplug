#!/usr/bin/env python3
"""Own, verify and tear down the disposable hybrid scenario from a checkout."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
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
        if label.startswith("building source-current "):
            # Build output can contain dependency configuration. Retain it only
            # in a private local directory; never print it or publish it as evidence.
            directory = RUNS / ("build-failure-" + uuid.uuid4().hex[:12])
            directory.mkdir(parents=True, mode=0o700)
            path = directory / "diagnostic.private.json"
            with path.open("x", encoding="utf-8") as stream:
                json.dump(
                    {
                        "command": args,
                        "returncode": result.returncode,
                        "stdout": result.stdout,
                        "stderr": result.stderr,
                    },
                    stream,
                    indent=2,
                )
                stream.write("\n")
            path.chmod(0o600)
            raise ScenarioFailure(
                f"{label} returned status {result.returncode}; private diagnostic: {path}"
            )
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
        self.fault = "none"
        self.environment = dict(os.environ)
        self.environment.update(
            ENTRYPLUG_COMPOSE_PROJECT=project,
            ENTRYPLUG_FIXTURE_PRIVATE=str(self.private),
            ENTRYPLUG_SCENARIO_RUN_ID=run_id,
            ENTRYPLUG_RUNS_DIR=str(RUNS),
            ENTRYPLUG_FIXTURE_UID=str(os.getuid()),
            ENTRYPLUG_FIXTURE_GID=str(os.getgid()),
        )

    def compose(self, *arguments: str, hybrid: bool = False, label: str) -> str:
        files = ["-f", str(COMPOSE)]
        if hybrid:
            files += ["-f", str(HYBRID)]
            if self.fault == "kill-active-worker":
                files += ["-f", str(ROOT / "containers/scenarios/compose.worker-loss.yaml")]
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
        if report.get("status") not in {"passed", "completed"}:
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


def _prepare_images(
    *, build: bool, native_worker: bool = True, mixed: bool = True
) -> dict[str, object]:
    source_sha256 = _source_digest()
    images: dict[str, str] = {}
    selected_images = [
        ("containers/harbor/Dockerfile", "entryplug-harbor:jazzy"),
    ]
    if mixed:
        selected_images.append(
            ("containers/mixed/Dockerfile.harbor", "entryplug-harbor-mixed:jazzy")
        )
    if native_worker:
        selected_images.append(
            ("containers/scenarios/Dockerfile.worker", "entryplug-scenario-worker:dev")
        )
    for dockerfile, tag in selected_images:
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
    if _source_digest() != source_sha256:
        raise ScenarioFailure("source changed during image preparation; rerun with --build")
    return {"source_sha256": source_sha256, "image_ids": images}


def _stop_native_worker(stack: OwnedStack, run_dir: Path) -> None:
    expected = [("native-worker", "worker-a", 0)]
    if stack.fault == "kill-active-worker":
        expected = [("native-worker", "worker-a", 137), ("native-worker-b", "worker-b", 0)]
    workers = []
    for service, worker_id, exit_code in expected:
        stack.compose(
            "stop", "--timeout", "20", service, hybrid=True, label=f"stopping owned {worker_id}"
        )
        identifier = stack.compose(
            "ps", "--all", "--quiet", service, hybrid=True, label=f"locating owned {worker_id}"
        ).strip()
        if not identifier or "\n" in identifier:
            raise ScenarioFailure("native worker ownership is ambiguous")
        state = json.loads(
            _command(
                ["docker", "inspect", "--format", "{{json .State}}", identifier],
                "checking native worker shutdown",
            )
        )
        if state.get("Running") is not False or state.get("ExitCode") != exit_code:
            raise ScenarioFailure("native worker shutdown disagrees with the declared fault")
        workers.append({"worker_id": worker_id, "exit_code": exit_code})
    with (run_dir / "worker-cleanup.json").open("x", encoding="utf-8") as stream:
        json.dump({"status": "stopped", "workers": workers}, stream)
        stream.write("\n")


def _check_secret(run_dir: Path, token_file: Path) -> None:
    secret = token_file.read_bytes().strip()
    if not secret:
        raise ScenarioFailure("fixture token is empty")
    for artifact in run_dir.iterdir():
        if artifact.is_file() and secret in artifact.read_bytes():
            raise ScenarioFailure("fixture token leaked into task evidence")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ScenarioFailure(message)


def _number(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _check_inspection_evidence(
    run_dir: Path, report: dict[str, object], *, profile: str = "hybrid"
) -> None:
    """Check the declared no-fault profile, separately from startup/repair cases."""
    _require(
        profile in {"hybrid", "hybrid-repair", "zenoh", "mqtt"}, "unsupported evaluator profile"
    )
    operations = report["operations"]
    levels = (0.25, 0.5, 0.75) if profile != "zenoh" else (0.75,)
    states = report.get("applied_states")
    _require(
        isinstance(states, list) and len(states) == len(levels), "missing applied light evidence"
    )
    prior_revision, prior_stamp = -1, -1.0
    for state, level in zip(states, levels, strict=True):
        _require(isinstance(state, dict), "malformed applied light evidence")
        revision, stamp, applied = (
            state.get("revision"),
            state.get("sim_time_s"),
            state.get("level"),
        )
        _require(
            type(revision) is int and revision > prior_revision, "lamp revision did not progress"
        )
        _require(_number(stamp) and stamp > prior_stamp, "lamp application time did not progress")
        _require(_number(applied) and abs(applied - level) <= 0.01, "wrong applied light level")
        prior_revision, prior_stamp = revision, stamp
    frames = report.get("frames")
    _require(isinstance(frames, list) and bool(frames), "missing frame evidence")
    indexed = {}
    for frame in frames:
        _require(isinstance(frame, dict), "malformed frame evidence")
        sample_id = frame.get("sample_id")
        _require(
            isinstance(sample_id, str) and bool(sample_id) and sample_id not in indexed,
            "duplicate or missing frame identity",
        )
        indexed[sample_id] = frame
    last_sequence, last_capture = -1, prior_stamp
    jobs = set()
    for index, operation in enumerate(operations):
        _require(
            operation.get("capability") == "inspect_target"
            and operation.get("motion_state") == "idle"
            and operation.get("effect_state")
            == ("reported" if index == 0 and profile != "zenoh" else "none"),
            "incorrect capability or physical effect",
        )
        result = operation["result"]
        for key, expected in {
            "target_id": "bench-marker",
            "source_id": "panel-camera",
            "lineage_id": "panel-camera-rgb8-v1",
            "program_id": "fixed-red-centroid-v1",
        }.items():
            _require(result.get(key) == expected, f"wrong {key}")
        _require(
            type(result.get("binding_revision")) is int
            and result["binding_revision"] == (2 if profile == "hybrid-repair" else 1)
            and type(result.get("worker_replacements")) is int
            and result["worker_replacements"] == (1 if profile == "hybrid-repair" else 0),
            "wrong no-fault binding",
        )
        quality, centroid = result.get("quality"), result.get("centroid_px")
        _require(_number(quality) and 0.5 <= quality <= 1, "invalid observation quality")
        _require(
            isinstance(centroid, list)
            and len(centroid) == 2
            and all(_number(v) for v in centroid)
            and 0 <= centroid[0] < 320
            and 0 <= centroid[1] < 240,
            "invalid centroid",
        )
        sample_ids, verification = result.get("sample_ids"), result.get("verification")
        _require(
            isinstance(sample_ids, list)
            and len(sample_ids) == 2
            and all(isinstance(v, str) and bool(v) for v in sample_ids)
            and len(set(sample_ids)) == 2,
            "missing distinct verifying samples",
        )
        _require(isinstance(verification, dict), "missing freshness evidence")
        _require(
            verification.get("clock") == "runtime_local_monotonic"
            and verification.get("maximum_frame_age_s") == 0.5
            and verification.get("minimum_quality") == 0.5,
            "wrong verification profile",
        )
        accepted, samples = verification.get("accepted_monotonic"), verification.get("samples")
        _require(
            _number(accepted) and isinstance(samples, list) and len(samples) == 2,
            "malformed freshness evidence",
        )
        for sample_id, sample in zip(sample_ids, samples, strict=True):
            _require(
                isinstance(sample, dict) and sample.get("sample_id") == sample_id,
                "verification sample identity mismatch",
            )
            frame = indexed.get(sample_id)
            _require(isinstance(frame, dict), "verifying frame missing")
            _require(
                frame.get("source_id") == result["source_id"]
                and frame.get("lineage_id") == result["lineage_id"],
                "frame source mismatch",
            )
            sequence = sample.get("sequence")
            _require(
                type(sequence) is int
                and sequence > last_sequence
                and sequence == frame.get("sequence"),
                "frame sequence did not progress",
            )
            stamp = frame.get("captured_sim_time_s")
            _require(
                _number(stamp) and stamp > last_capture,
                "frame predates application or prior capture",
            )
            received, age = sample.get("received_monotonic"), sample.get("age_at_acceptance_s")
            _require(
                _number(received)
                and _number(age)
                and 0 <= age <= 0.5
                and math.isclose(accepted - received, age, abs_tol=1e-9)
                and received == frame.get("received_monotonic"),
                "stale or inconsistent frame age",
            )
            _require(
                _number(sample.get("quality")) and 0.5 <= sample["quality"] <= 1,
                "invalid verifying sample quality",
            )
            if profile == "mqtt":
                _require(
                    result.get("worker_id") == "local-panel-worker-v1"
                    and all(
                        sample.get(k) is None
                        for k in ("worker_generation", "job_id", "input_sha256")
                    ),
                    "local detector must not claim native worker evidence",
                )
            else:
                _require(
                    type(sample.get("worker_generation")) is int
                    and sample["worker_generation"] == 1,
                    "wrong worker generation",
                )
                job = sample.get("job_id")
                _require(
                    isinstance(job, str) and bool(job) and job not in jobs,
                    "missing or reused compute job",
                )
                jobs.add(job)
                digest = sample.get("input_sha256")
                _require(
                    isinstance(digest, str)
                    and re.fullmatch(r"[a-f0-9]{64}", digest) is not None
                    and digest == frame.get("input_sha256"),
                    "compute input mismatch",
                )
            name = frame.get("artifact")
            _require(
                isinstance(name, str)
                and Path(name).name == name
                and name in report.get("frame_artifacts", []),
                "invalid raw frame reference",
            )
            path = run_dir / name
            _require(not path.is_symlink() and path.is_file(), "raw frame missing")
            content = path.read_bytes()
            _require(
                content.startswith(b"\x89PNG\r\n\x1a\n")
                and hashlib.sha256(content).hexdigest() == frame.get("artifact_sha256"),
                "raw frame digest mismatch",
            )
            last_sequence, last_capture = sequence, stamp
        _require(result.get("last_sequence") == last_sequence, "result sequence mismatch")
    if profile == "mqtt":
        return  # Local detector lives and closes inside the owned evaluator process.
    cleanup_path = run_dir / "worker-cleanup.json"
    _require(cleanup_path.is_file(), "missing native worker cleanup evidence")
    cleanup = json.loads(cleanup_path.read_text())
    expected_workers = [{"worker_id": "worker-a", "exit_code": 0}]
    if profile == "hybrid-repair":
        expected_workers = [
            {"worker_id": "worker-a", "exit_code": 137},
            {"worker_id": "worker-b", "exit_code": 0},
        ]
    _require(
        cleanup == {"status": "stopped", "workers": expected_workers},
        "native worker did not stop cleanly",
    )


def _check_mission_evidence(report: dict[str, object]) -> None:
    mission = report.get("mission")
    _require(isinstance(mission, dict) and bool(mission.get("id")), "missing mission identity")
    runs = report.get("mission_runs", [])
    application = report.get("application_operations", [])
    native = report.get("operations", [])
    _require(len(runs) == len(application) == len(native) == 2, "missing mission operations")
    _require(
        all(
            run.get("mission_id") == mission["id"]
            and run.get("lifecycle") == "completed"
            and run.get("model_calls") == 0
            and run.get("tool_calls") == 1
            for run in runs
        ),
        "mission did not complete with one no-model task call",
    )
    evidence = {item["id"]: item for item in report.get("mission_evidence", [])}
    for run, op, result in zip(runs, application, native, strict=True):
        _require(
            op.get("run_id") == run.get("id")
            and op.get("native_operation_id") == result.get("operation_id")
            and op.get("lifecycle") == result.get("lifecycle") == "succeeded"
            and op.get("result") == result.get("result")
            and op.get("physical_effects") is True
            and bool(op.get("evidence_ids"))
            and all(
                identifier in evidence
                and evidence[identifier].get("operation_id") == op.get("id")
                and evidence[identifier].get("content") == result
                for identifier in op["evidence_ids"]
            ),
            "mission and native task evidence disagree",
        )


def _check_lost_ha_result(
    run_dir: Path, token_file: Path, *, canceled: bool = False
) -> dict[str, object]:
    report = json.loads((run_dir / "hybrid.json").read_text())
    bridge = json.loads((run_dir / "bridge.json").read_text())
    fault = report.get("fault", {})
    runs = report.get("mission_runs", [])
    application = report.get("application_operations", [])
    operations = report.get("operations", [])
    _require(len(runs) == len(application) == len(operations) == 1, "expected one uncertain task")
    run, op, native = runs[0], application[0], operations[0]
    _require(
        run.get("health") == "blocked"
        and run.get("lifecycle") != "completed"
        and run.get("reason_code") == "LIGHT_COMMAND_UNCONFIRMED"
        and run.get("model_calls") == 0
        and run.get("tool_calls") == 1
        and op.get("run_id") == run.get("id")
        and op.get("native_operation_id") == native.get("operation_id")
        and op.get("lifecycle") == native.get("lifecycle") == "indeterminate"
        and op.get("effect_state") == native.get("effect_state") == "unknown"
        and op.get("result") == native.get("result")
        and native.get("reason_code") == "LIGHT_COMMAND_UNCONFIRMED"
        and native.get("result", {}).get("lighting_writes") == 1,
        "mission did not retain native physical uncertainty",
    )
    applied = fault.get("applied", {})
    _require(
        fault.get("service_calls") == fault.get("dropped_results") == 1
        and fault.get("upstream_success") is True
        and fault.get("entity_id") == report.get("home_assistant_entity")
        and fault.get("brightness") == 64
        and type(fault.get("command_id")) is int
        and _number(applied.get("level"))
        and abs(applied["level"] - 64 / 255) < 0.01
        and _number(applied.get("sim_time_s"))
        and type(applied.get("prior_revision")) is int
        and type(applied.get("revision")) is int
        and applied["revision"] > applied["prior_revision"]
        and bridge.get("status") == "stopped"
        and bridge.get("applied_count") == 1,
        "lost reply was not tied to exactly one real applied light command",
    )
    _require(
        fault.get("duplicate_application_id") == op.get("id")
        and fault.get("duplicate_native_id") == native.get("operation_id")
        and fault.get("application_retry_refusal") == "admission_closed"
        and fault.get("native_retry_refusal") == "EFFECT_INHIBITED"
        and fault.get("effect_inhibited_reason") == "LIGHT_COMMAND_UNCONFIRMED",
        "uncertain physical action was not fenced against retries",
    )
    evidence = {item["id"]: item for item in report.get("mission_evidence", [])}
    _require(
        bool(op.get("evidence_ids"))
        and all(
            identifier in evidence
            and evidence[identifier].get("content") == native
            and evidence[identifier].get("operation_id") == op.get("id")
            for identifier in op["evidence_ids"]
        ),
        "uncertain operation is missing durable evidence",
    )
    _require(
        json.loads((run_dir / "worker-cleanup.json").read_text())
        == {"status": "stopped", "workers": [{"worker_id": "worker-a", "exit_code": 0}]},
        "worker did not stop cleanly",
    )
    _require(
        json.loads((run_dir / "world-cleanup.json").read_text())
        == {"status": "stopped", "forced_stops": 0},
        "world did not stop cleanly",
    )
    if canceled:
        requested = fault.get("cancel_requested_monotonic")
        terminal = fault.get("terminal_observed_monotonic")
        _require(
            fault.get("cancel_before_reply_loss") is True
            and fault.get("canceled_native_id") == native.get("operation_id")
            and native.get("cancel_requested") is True
            and op.get("cancel_requested") is True
            and _number(requested)
            and _number(terminal)
            and 0 <= terminal - requested < 10
            and fault.get("terminal_before_repeated_cancel") == native
            and fault.get("terminal_after_repeated_cancel") == native,
            "cancellation did not preserve bounded terminal uncertainty",
        )
    _check_secret(run_dir, token_file)
    return {
        "status": "passed",
        "operation_ids": [native["operation_id"]],
        "effect_state": "unknown",
        "bridge_applied_count": 1,
        "mission_health": "blocked",
        "native_retry_refusal": "EFFECT_INHIBITED",
    }


def _check_result(
    run_dir: Path, token_file: Path, *, repair: bool = False, mission: bool = False
) -> dict[str, object]:
    report = json.loads((run_dir / "hybrid.json").read_text(encoding="utf-8"))
    bridge = json.loads((run_dir / "bridge.json").read_text(encoding="utf-8"))
    if report.get("status") not in {"passed", "completed"} or bridge.get("status") != "stopped":
        raise ScenarioFailure("hybrid task or world-owned bridge did not pass")
    operations = report.get("operations")
    if not isinstance(operations, list) or len(operations) != 2:
        raise ScenarioFailure("hybrid task did not return two operations")
    if not all(isinstance(operation, dict) for operation in operations):
        raise ScenarioFailure("malformed operation evidence")
    first, warm = (operation.get("result") for operation in operations)
    if (
        any(operation.get("lifecycle") != "succeeded" for operation in operations)
        or operations[0].get("operation_id") == operations[1].get("operation_id")
        or not isinstance(first, dict)
        or not isinstance(warm, dict)
        or first.get("worker_id") != warm.get("worker_id")
        or first.get("worker_id") != ("worker-b" if repair else "worker-a")
        or first.get("lighting_writes") != 3
        or warm.get("lighting_writes") != 0
        or set(first.get("sample_ids", [])) & set(warm.get("sample_ids", []))
        or bridge.get("applied_count") != first.get("lighting_writes")
    ):
        raise ScenarioFailure("hybrid task did not prove fresh checked reuse")
    _check_inspection_evidence(run_dir, report, profile="hybrid-repair" if repair else "hybrid")
    if repair:
        for operation in operations:
            timing = operation.get("timing", {})
            accepted = timing.get("accepted_monotonic")
            deadline = timing.get("accepted_deadline_monotonic")
            completed = timing.get("completed_monotonic")
            _require(
                _number(accepted)
                and _number(deadline)
                and _number(completed)
                and timing.get("final_deadline_monotonic") == deadline
                and math.isclose(deadline - accepted, 30.0)
                and accepted <= completed <= deadline,
                "repair changed or exceeded the operation deadline",
            )
        fault = json.loads((run_dir / "fault.json").read_text())
        identity = fault.get("result_identity", {})
        _require(
            fault.get("phase") == "before_native_reply"
            and fault.get("job_number") == 3
            and fault.get("signal") == "SIGKILL"
            and identity.get("worker_id") == "worker-a"
            and identity.get("worker_generation") == 1
            and identity.get("program_id") == "fixed-red-centroid-v1",
            "active worker fault was not observed",
        )
        _require(
            identity.get("sample_id") in {frame["sample_id"] for frame in report["frames"]},
            "fault does not belong to this camera run",
        )
    client_path = run_dir / "client.json"
    if client_path.is_file():
        client = json.loads(client_path.read_text(encoding="utf-8"))
        client_ops = client.get("operations", [])
        if [item.get("operation_id") for item in client_ops] != [
            item.get("operation_id") for item in operations
        ]:
            raise ScenarioFailure("client and operation host disagree about task identity")
    if mission or "mission" in report:
        _check_mission_evidence(report)
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
    if (
        report.get("status") not in {"failed", "completed"}
        or not isinstance(operations, list)
        or len(operations) != 1
    ):
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


def _run_local_session(stack: OwnedStack, run_dir: Path, run_id: str) -> None:
    """Drive the same operation host through a local agent-facing Session socket."""

    from hybrid_session_client import run_scripted

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
        "session",
        hybrid=True,
        label="starting bounded hybrid Session",
    )
    ready = run_dir / "session-ready.json"
    deadline = time.monotonic() + 90.0
    while not ready.is_file():
        if time.monotonic() >= deadline:
            raise ScenarioFailure("hybrid Session did not become ready")
        time.sleep(0.25)
    if stack.fault == "kill-active-worker":
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(run_scripted, run_dir / "session.sock", run_id)
            held = stack.private / "compute-control" / "job-held.json"
            while not held.is_file():
                if future.done() or time.monotonic() >= deadline:
                    raise ScenarioFailure("primary did not reach the declared active job")
                time.sleep(0.02)
            fault = json.loads(held.read_text())
            stack.compose(
                "kill",
                "--signal",
                "SIGKILL",
                "native-worker",
                hybrid=True,
                label="killing owned primary before native reply",
            )
            with (run_dir / "fault.json").open("x") as stream:
                json.dump({**fault, "signal": "SIGKILL"}, stream)
            client_report = future.result(timeout=max(1, deadline - time.monotonic()))
    else:
        client_report = run_scripted(run_dir / "session.sock", run_id)
    with (run_dir / "client.json").open("x", encoding="utf-8") as stream:
        json.dump(client_report, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
    while not (run_dir / "hybrid.json").is_file() or not (run_dir / "bridge.json").is_file():
        if time.monotonic() >= deadline:
            raise ScenarioFailure("hybrid Session did not finish owned teardown")
        time.sleep(0.25)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--build", action="store_true", help="build source-current images")
    parser.add_argument(
        "--fault",
        choices=(
            "none",
            "no-native-worker",
            "kill-active-worker",
            "ha-result-loss",
            "ha-cancel-write",
        ),
        default="none",
    )
    parser.add_argument("--client", choices=("direct", "session", "mission"), default="direct")
    args = parser.parse_args()
    if args.client == "mission" and args.fault not in {"none", "ha-result-loss", "ha-cancel-write"}:
        parser.error("mission mode supports no-fault, HA result loss and HA write cancellation")
    if args.fault in {"ha-result-loss", "ha-cancel-write"} and args.client != "mission":
        parser.error("HA faults require --client mission")
    if args.fault == "kill-active-worker" and args.client != "session":
        parser.error("active-worker loss requires --client session")
    RUNS.mkdir(exist_ok=True)
    run_id = "hybrid-" + uuid.uuid4().hex[:12]
    run_dir = RUNS / run_id
    bootstraps: list[dict[str, object]] = []
    outcome: dict[str, object] = {
        "status": "failed",
        "run_id": run_id,
        "fault": args.fault,
        "client": args.client,
        "fresh_volume_bootstraps": bootstraps,
    }
    stack: OwnedStack | None = None
    try:
        outcome.update(_prepare_images(build=args.build))
        for project in (f"ep-{run_id}-proof", f"ep-{run_id}"):
            stack = OwnedStack(project, run_id, pull_images=args.build)
            stack.fault = args.fault
            if args.fault == "kill-active-worker":
                (stack.private / "compute-control").mkdir(mode=0o700)
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
                if args.fault != "no-native-worker":
                    services.insert(1, "native-worker")
                if args.fault == "kill-active-worker":
                    services.insert(2, "native-worker-b")
                stack.compose(
                    "up",
                    "-d",
                    "--no-build",
                    *services,
                    hybrid=True,
                    label="starting native compute and ROS workbench",
                )
                if args.client == "session":
                    _run_local_session(stack, run_dir, run_id)
                    if args.fault == "no-native-worker":
                        outcome.update(
                            _check_unavailable_result(run_dir, stack.private / "ha-token")
                        )
                    else:
                        _stop_native_worker(stack, run_dir)
                        outcome.update(
                            _check_result(
                                run_dir,
                                stack.private / "ha-token",
                                repair=args.fault == "kill-active-worker",
                            )
                        )
                    continue
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
                        "mission-cancel-write"
                        if args.fault == "ha-cancel-write"
                        else "mission-lost-reply"
                        if args.fault == "ha-result-loss"
                        else args.client,
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
                elif args.fault in {"ha-result-loss", "ha-cancel-write"}:
                    _stop_native_worker(stack, run_dir)
                    outcome.update(
                        _check_lost_ha_result(
                            run_dir,
                            stack.private / "ha-token",
                            canceled=args.fault == "ha-cancel-write",
                        )
                    )
                else:
                    _stop_native_worker(stack, run_dir)
                    outcome.update(
                        _check_result(
                            run_dir,
                            stack.private / "ha-token",
                            repair=args.fault == "kill-active-worker",
                            mission=args.client == "mission",
                        )
                    )
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
