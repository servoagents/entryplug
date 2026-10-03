#!/usr/bin/env python3
"""Sequential source-checkout scenarios with one preflight and retained lane results."""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from run_hybrid import ROOT, RUNS, ScenarioFailure, _command, _prepare_images, _source_digest

# Compose is the authority for service images; do not copy its pinned digests here.
LANES = {
    "zenoh": ("compose.zenoh.yaml",),
    "mqtt": ("compose.mqtt.yaml",),
    "ros2-vision": (),
    "ros2-recovery": (),
    "hybrid": ("compose.yaml", "compose.hybrid.yaml"),
}
ACCEPTANCE_BLOCKERS = (
    "ROS recovery authority matrix and release code/image/profile freeze are incomplete",
    "mixed-resource required faults, development profiles and freeze are incomplete",
    "strong direct/reuse comparisons and frozen paired evaluation are not implemented",
)


def preflight(selected: list[str], *, build: bool, offline: bool) -> dict[str, object]:
    """Resolve every required image before any fixture starts; offline never provisions."""
    if offline and build:
        raise ScenarioFailure("--offline cannot be combined with --build")
    environment = dict(os.environ)
    environment.update(
        ENTRYPLUG_COMPOSE_PROJECT="ep-preflight",
        ENTRYPLUG_FIXTURE_PRIVATE="/tmp/entryplug-preflight-unused",
        ENTRYPLUG_SCENARIO_RUN_ID="preflight",
        ENTRYPLUG_RUNS_DIR=str(RUNS),
        ENTRYPLUG_FIXTURE_UID=str(os.getuid()),
        ENTRYPLUG_FIXTURE_GID=str(os.getgid()),
    )
    docker = _command(["docker", "version", "--format", "{{.Server.Version}}"], "checking Docker")
    images: set[str] = set()
    compose = None
    for lane in selected:
        if not LANES[lane]:
            continue
        if compose is None:
            compose = _command(["docker", "compose", "version", "--short"], "checking Compose")
        command = ["docker", "compose"]
        for filename in LANES[lane]:
            command.extend(("-f", str(ROOT / "containers/scenarios" / filename)))
        output = _command(
            [*command, "config", "--images"],
            f"resolving {lane} dependencies",
            environment=environment,
        )
        images.update(output.split())
    prepared = _prepare_images(
        build=build,
        mixed=any(lane in {"zenoh", "mqtt", "hybrid"} for lane in selected),
        native_worker=any(lane in {"zenoh", "hybrid"} for lane in selected),
    )
    services = {}
    for image in sorted(images - prepared["image_ids"].keys()):
        if not re.fullmatch(r"[^\s]+@sha256:[a-f0-9]{64}", image):
            raise ScenarioFailure(f"service image is not pinned: {image}")
        command = ["docker", "image", "inspect", "--format", "{{.Id}}", image]
        try:
            identity = _command(command, f"checking cached service {image}").strip()
        except ScenarioFailure as error:
            if not build:
                raise ScenarioFailure(
                    f"cached service {image} is missing; provision with --build without --offline"
                ) from error
            _command(["docker", "pull", image], f"provisioning {image}")
            identity = _command(command, f"checking provisioned service {image}").strip()
        services[image] = identity
    if _source_digest() != prepared["source_sha256"]:
        raise ScenarioFailure("source changed during preflight; rerun with --build")
    return {
        **prepared,
        "service_image_ids": services,
        "docker_version": docker.strip(),
        "compose_version": compose.strip() if compose else None,
        "architecture": os.uname().machine,
    }


def execute(command: list[str], stream: object) -> int:
    """Interrupt the owned runner gracefully so its fixture finally blocks can run."""
    with subprocess.Popen(command, cwd=ROOT, stdout=stream, start_new_session=True) as child:
        try:
            return child.wait()
        except KeyboardInterrupt:
            # Only this child receives SIGINT; its Docker commands and cleanup own
            # their own lifecycle. Do not kill a process group during teardown.
            child.send_signal(signal.SIGINT)
            print(f"waiting for owned scenario runner {child.pid} to clean up", file=sys.stderr)
            child.wait()
            raise


def run_lane(
    lane: str, directory: Path, identity: dict[str, object], *, client: str, fault: str
) -> dict[str, object]:
    """A zero exit alone is insufficient: require a new, matching persisted manifest."""
    row: dict[str, object] = {"status": "failed"}
    command = [
        sys.executable,
        str(ROOT / "containers/scenarios" / f"run_{lane.replace('-', '_')}.py"),
    ]
    if lane == "hybrid":
        command.extend(("--client", client, "--fault", fault))
    row["command"] = command
    prior = {path.name for path in RUNS.iterdir()}
    started = time.monotonic()
    output = directory / f"{lane}.stdout.json"
    try:
        if _source_digest() != identity["source_sha256"]:
            raise ScenarioFailure("source changed since preflight; no fixture launched")
        # Keep diagnostics on stderr; stdout contains exactly one suite manifest.
        with output.open("x") as stream:
            returncode = execute(command, stream)
        row["returncode"] = returncode
        report = json.loads(output.read_text())
        if not isinstance(report, dict):
            raise ScenarioFailure("lane did not return an object manifest")
        run_id = report.get("run_id")
        if (
            not isinstance(run_id, str)
            or re.fullmatch(re.escape(lane) + r"-[a-f0-9]{12}", run_id) is None
            or run_id in prior
        ):
            raise ScenarioFailure("lane did not produce a new owned run identity")
        evidence = RUNS / run_id / "scenario.json"
        if json.loads(evidence.read_text()) != report:
            raise ScenarioFailure("lane output disagrees with persisted evidence")
        row.update(evidence=str(evidence.relative_to(ROOT)), result=report)
        if report.get("source_sha256") != identity["source_sha256"]:
            raise ScenarioFailure("lane source identity differs from preflight")
        lane_images = report.get("image_ids")
        if (
            not isinstance(lane_images, dict)
            or not lane_images
            or any(identity["image_ids"].get(tag) != value for tag, value in lane_images.items())
        ):
            raise ScenarioFailure("lane image identity differs from preflight")
        if _source_digest() != identity["source_sha256"]:
            raise ScenarioFailure("source changed during lane execution")
        if returncode != 0 or report.get("status") != "passed":
            raise ScenarioFailure("lane did not pass both execution and evidence checks")
        row["status"] = "passed"
    except KeyboardInterrupt:
        row.update(status="interrupted", reason="KeyboardInterrupt")
        raise
    except (OSError, ValueError, ScenarioFailure) as error:
        row.update(reason=type(error).__name__, detail=str(error))
    finally:
        row["elapsed_s"] = time.monotonic() - started
        with (directory / f"{lane}.json").open("x") as stream:
            json.dump(row, stream, indent=2, allow_nan=False)
            stream.write("\n")
    return row


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=tuple(LANES))
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--tier", choices=("smoke", "acceptance"), default="smoke")
    parser.add_argument("--json", action="store_true", help="JSON is always emitted on stdout")
    parser.add_argument("--client", choices=("direct", "session"), default="direct")
    parser.add_argument(
        "--fault", choices=("none", "no-native-worker", "kill-active-worker"), default="none"
    )
    args = parser.parse_args(argv)
    if args.offline and args.build:
        parser.error("--offline cannot be combined with --build")
    if args.scenario != "hybrid" and (args.client != "direct" or args.fault != "none"):
        parser.error("--fault/--client require --scenario hybrid")
    if args.fault == "kill-active-worker" and args.client != "session":
        parser.error("--fault kill-active-worker requires --client session")
    selected = [args.scenario] if args.scenario else list(LANES)
    run_id = "scenarios-" + uuid.uuid4().hex[:12]
    directory = RUNS / run_id
    directory.mkdir(parents=True, mode=0o700)
    lanes = {
        name: {
            "status": "not_run",
            "reason": "preflight_pending" if name in selected else "unselected",
        }
        for name in LANES
    }
    outcome: dict[str, object] = {
        "status": "blocked",
        "run_id": run_id,
        "tier": args.tier,
        "phase": "development",
        "offline": args.offline,
        "selected": selected,
        "lanes": lanes,
        "claim_boundary": "Source-checkout development scenarios; no acceptance or holdout claim.",
    }
    code = 2
    try:
        if args.tier == "acceptance":
            outcome["prerequisites"] = list(ACCEPTANCE_BLOCKERS)
            for lane in selected:
                lanes[lane]["reason"] = "acceptance_prerequisites_incomplete"
        else:
            identity = preflight(selected, build=args.build, offline=args.offline)
            outcome.update(identity)
            for lane in selected:
                print(f"scenario suite: {lane}", file=sys.stderr, flush=True)
                lanes[lane] = run_lane(
                    lane,
                    directory,
                    identity,
                    client="session" if args.scenario is None else args.client,
                    fault=args.fault,
                )
                print(
                    f"scenario suite: {lane} {lanes[lane]['status']}", file=sys.stderr, flush=True
                )
            outcome["status"] = (
                "passed"
                if all(lanes[lane]["status"] == "passed" for lane in selected)
                else "failed"
            )
            code = 0 if outcome["status"] == "passed" else 1
    except KeyboardInterrupt:
        outcome.update(status="interrupted", reason="KeyboardInterrupt")
        code = 130
        for lane in selected:
            if lanes[lane]["status"] == "not_run":
                saved = directory / f"{lane}.json"
                lanes[lane] = (
                    json.loads(saved.read_text())
                    if saved.exists()
                    else {"status": "not_run", "reason": "suite_interrupted"}
                )
    except (OSError, ValueError, ScenarioFailure, subprocess.SubprocessError) as error:
        outcome.update(reason=type(error).__name__, detail=str(error))
        for lane in selected:
            if lanes[lane]["status"] == "not_run":
                lanes[lane]["reason"] = "preflight_failed"
    finally:
        with (directory / "scenario.json").open("x") as stream:
            json.dump(outcome, stream, indent=2, allow_nan=False)
            stream.write("\n")
    print(json.dumps(outcome, sort_keys=True, allow_nan=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
