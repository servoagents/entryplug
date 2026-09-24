"""Container runtime orchestration for Entryplug qualification cases."""

from __future__ import annotations

import os
import re
import subprocess
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_HARBOR_IMAGE = "entryplug-harbor:jazzy"


@dataclass(frozen=True, slots=True)
class RuntimeResult:
    """Outcome and evidence location for one isolated runtime invocation."""

    returncode: int
    run_id: str
    evidence_path: Path


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
) -> list[str]:
    """Construct the least-privilege command for a self-contained Harbor case."""

    runs = (root / "runs").resolve()
    container_run = f"/workspace/entryplug/runs/{run_id}"
    command = [
        "docker",
        "run",
        "--rm",
        "--init",
        "--name",
        f"entryplug-harbor-{run_id}",
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
        source = _reuse_cache_path(root, case, reuse_from)
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
    return command


def _reuse_cache_path(root: Path, case: str, reuse_from: str) -> Path:
    if case != "mirrors":
        raise ValueError("prior evidence reuse is only supported by the mirrors case")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", reuse_from) is None:
        raise ValueError("reuse run ID contains unsupported characters")
    runs = (root / "runs").resolve()
    cache = (runs / reuse_from / "evidence-cache.private.sqlite3").resolve()
    if not cache.is_relative_to(runs):
        raise ValueError("reuse cache resolves outside the runs directory")
    if not cache.is_file():
        raise FileNotFoundError(f"prior evidence cache is unavailable: {cache}")
    return cache


def run_harbor(
    root: Path,
    *,
    image: str = DEFAULT_HARBOR_IMAGE,
    case: str = "render-smoke",
    reuse_from: str | None = None,
    build: bool = False,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> RuntimeResult:
    """Build when requested, then run a Harbor qualification case."""

    if case not in supported_cases():
        raise ValueError(f"unsupported Harbor case: {case}")
    if reuse_from is not None:
        _reuse_cache_path(root, case, reuse_from)
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

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"harbor-{case}-{stamp}-{uuid.uuid4().hex[:8]}"
    completed = runner(
        harbor_run_command(root, image, run_id, case, reuse_from),
        cwd=root,
        check=False,
        text=True,
    )
    return RuntimeResult(completed.returncode, run_id, runs / run_id)


def supported_cases() -> Sequence[str]:
    return ("render-smoke", "reach", "mirrors")
