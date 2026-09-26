"""Container runtime orchestration for Entryplug qualification cases."""

from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from entryplug.seeding import validate_experiment_seed

DEFAULT_HARBOR_IMAGE = "entryplug-harbor:jazzy"
RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


@dataclass(frozen=True, slots=True)
class RuntimeResult:
    """Outcome and evidence location for one isolated runtime invocation."""

    returncode: int
    run_id: str
    evidence_path: Path


def harbor_container_name(run_id: str) -> str:
    """Return the unique Docker name for one validated owned run."""

    if RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise ValueError("Harbor run ID contains unsupported characters")
    return f"entryplug-harbor-{run_id}"


def new_harbor_run_id(case: str, seed: int | None = None) -> str:
    """Create the evidence and container identity shared by all launch paths."""

    if case not in supported_cases() and case != "resident":
        raise ValueError(f"unsupported Harbor case: {case}")
    _validate_case_seed(case, seed)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    seed_label = f"-s{seed}" if seed is not None else ""
    return f"harbor-{case}{seed_label}-{stamp}-{uuid.uuid4().hex[:8]}"


def harbor_build_command(root: Path, image: str, uid: int, gid: int) -> list[str]:
    """Build the pinned Harbor image without depending on a shell."""

    return [
        "docker",
        "build",
        "--file",
        str(root / "containers" / "harbor" / "Dockerfile"),
        "--build-arg",
        f"ENTRYPLUG_UID={uid}",
        "--build-arg",
        f"ENTRYPLUG_GID={gid}",
        "--tag",
        image,
        str(root),
    ]


def harbor_run_command(
    root: Path,
    image: str,
    run_id: str,
    case: str = "render-smoke",
    reuse_from: str | None = None,
    seed: int | None = None,
) -> list[str]:
    """Construct the least-privilege command for a self-contained Harbor case."""

    runs = (root / "runs").resolve()
    _validate_case_seed(case, seed)
    container_name = harbor_container_name(run_id)
    container_run = f"/workspace/entryplug/runs/{run_id}"
    command = [
        "docker",
        "run",
        "--rm",
        "--init",
        "--name",
        container_name,
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--pids-limit=512",
        "--shm-size=512m",
        "--network=none",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=512m",
        "--tmpfs",
        "/home/entryplug/.ros:rw,nosuid,nodev,size=32m",
        "--tmpfs",
        "/home/entryplug/.cache:rw,nosuid,nodev,size=64m",
        "--env",
        "PYTHONNOUSERSITE=1",
        "--env",
        "RMW_IMPLEMENTATION=rmw_zenoh_cpp",
        "--mount",
        f"type=bind,src={runs},dst=/workspace/entryplug/runs",
    ]
    if reuse_from is not None:
        source = _reuse_cache_path(root, case, reuse_from, seed)
        container_source = f"/workspace/entryplug/runs/{reuse_from}/evidence-cache.private.sqlite3"
        command.extend(
            (
                "--mount",
                f"type=bind,src={source},dst={container_source},readonly",
            )
        )
    command.extend(
        (
            image,
            "/usr/local/bin/entryplug-harbor-smoke",
            container_run,
            case,
        )
    )
    if reuse_from is not None:
        command.append(reuse_from)
    if seed is not None:
        if reuse_from is None:
            command.append("")
        command.append(str(seed))
    return command


def _validate_case_seed(case: str, seed: int | None) -> None:
    if seed is None:
        return
    if case != "mirrors":
        raise ValueError("experiment seeds are only supported by the mirrors case")
    validate_experiment_seed(seed)


def _reuse_cache_path(
    root: Path,
    case: str,
    reuse_from: str,
    seed: int | None = None,
) -> Path:
    if case != "mirrors":
        raise ValueError("prior evidence reuse is only supported by the mirrors case")
    if RUN_ID_PATTERN.fullmatch(reuse_from) is None:
        raise ValueError("reuse run ID contains unsupported characters")
    runs = (root / "runs").resolve()
    cache = (runs / reuse_from / "evidence-cache.private.sqlite3").resolve()
    if not cache.is_relative_to(runs):
        raise ValueError("reuse cache resolves outside the runs directory")
    if not cache.is_file():
        raise FileNotFoundError(f"prior evidence cache is unavailable: {cache}")
    summary = cache.parent / "mirrors.json"
    if not summary.is_file():
        raise FileNotFoundError(f"prior mirrors result is unavailable: {summary}")
    try:
        result = json.loads(summary.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"prior mirrors result is unreadable: {summary}") from error
    if not isinstance(result, dict) or result.get("status") != "passed":
        raise ValueError("prior mirrors run did not pass qualification")
    recorded_seeds = result.get("seeds")
    recorded_seed = recorded_seeds.get("experiment") if isinstance(recorded_seeds, dict) else None
    if recorded_seed != seed:
        raise ValueError("prior mirrors run used a different experiment seed")
    return cache


def run_harbor(
    root: Path,
    *,
    image: str = DEFAULT_HARBOR_IMAGE,
    case: str = "render-smoke",
    reuse_from: str | None = None,
    seed: int | None = None,
    build: bool = False,
    capture_output: bool = False,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> RuntimeResult:
    """Build when requested, then run a Harbor qualification case."""

    if case not in supported_cases():
        raise ValueError(f"unsupported Harbor case: {case}")
    _validate_case_seed(case, seed)
    if reuse_from is not None:
        _reuse_cache_path(root, case, reuse_from, seed)
    runs = root / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    if build:
        built = runner(
            harbor_build_command(root, image, os.getuid() or 1000, os.getgid() or 1000),
            cwd=root,
            check=False,
            text=True,
        )
        if built.returncode != 0:
            raise RuntimeError(f"container image build failed with status {built.returncode}")
    else:
        inspected = runner(
            ["docker", "image", "inspect", image],
            cwd=root,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        if inspected.returncode != 0:
            raise FileNotFoundError(f"container image {image!r} is unavailable; rerun with --build")

    run_id = new_harbor_run_id(case, seed)
    output_options: dict[str, object] = {}
    if capture_output:
        output_options = {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT}
    completed = runner(
        harbor_run_command(root, image, run_id, case, reuse_from, seed),
        cwd=root,
        check=False,
        text=True,
        **output_options,
    )
    evidence_path = runs / run_id
    if capture_output and completed.stdout is not None:
        evidence_path.mkdir(parents=True, exist_ok=True)
        with (evidence_path / "launcher.log").open("x", encoding="utf-8") as stream:
            stream.write(completed.stdout)
    return RuntimeResult(completed.returncode, run_id, evidence_path)


def supported_cases() -> Sequence[str]:
    return ("render-smoke", "reach", "mirrors")
