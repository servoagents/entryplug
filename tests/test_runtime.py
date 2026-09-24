from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from entryplug.runtime import (
    harbor_build_command,
    harbor_run_command,
    run_harbor,
    supported_cases,
)


def test_harbor_build_is_explicit_and_pins_the_dockerfile(tmp_path: Path) -> None:
    command = harbor_build_command(tmp_path, "entryplug:test", 123, 456)

    assert command[:2] == ["docker", "build"]
    assert str(tmp_path / "containers" / "harbor" / "Dockerfile") in command
    assert "ENTRYPLUG_UID=123" in command
    assert "ENTRYPLUG_GID=456" in command


def test_harbor_run_drops_privilege_and_only_mounts_evidence(tmp_path: Path) -> None:
    command = harbor_run_command(tmp_path, "entryplug:test", "run-123", "reach")
    joined = " ".join(command)

    assert "--read-only" in command
    assert "--cap-drop=ALL" in command
    assert "--security-opt=no-new-privileges" in command
    assert "--network=none" in command
    assert "docker.sock" not in joined
    assert "src=" + str((tmp_path / "runs").resolve()) in joined
    assert "type=bind,src=" + str(Path.home()) not in joined
    assert command[-1] == "reach"


def test_harbor_mounts_prior_mirror_evidence_read_only(tmp_path: Path) -> None:
    run_id = "harbor-mirrors-prior"
    cache = tmp_path / "runs" / run_id / "evidence-cache.private.sqlite3"
    cache.parent.mkdir(parents=True)
    cache.touch()

    command = harbor_run_command(
        tmp_path,
        "entryplug:test",
        "harbor-mirrors-current",
        "mirrors",
        run_id,
    )

    expected_mount = (
        f"type=bind,src={cache.resolve()},"
        f"dst=/workspace/entryplug/runs/{run_id}/evidence-cache.private.sqlite3,readonly"
    )
    assert expected_mount in command
    assert command[-3:] == [
        "/workspace/entryplug/runs/harbor-mirrors-current",
        "mirrors",
        run_id,
    ]


def test_harbor_rejects_unsafe_or_inapplicable_reuse_before_side_effects(
    tmp_path: Path,
) -> None:
    def runner(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"unexpected runtime call: {command}")

    for case, run_id in (("mirrors", "../escape"), ("reach", "prior")):
        try:
            run_harbor(tmp_path, case=case, reuse_from=run_id, runner=runner)
        except ValueError:
            pass
        else:
            raise AssertionError("an invalid reuse source was accepted")
    assert not (tmp_path / "runs").exists()


def test_harbor_rejects_reuse_cache_symlink_outside_runs(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "evidence-cache.private.sqlite3").touch()
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "linked-run").symlink_to(outside, target_is_directory=True)

    try:
        harbor_run_command(
            tmp_path,
            "entryplug:test",
            "current",
            "mirrors",
            "linked-run",
        )
    except ValueError as error:
        assert "outside the runs directory" in str(error)
    else:
        raise AssertionError("a reuse cache outside runs was accepted")


def test_harbor_exposes_reaching_and_rejects_unknown_cases_before_side_effects(
    tmp_path: Path,
) -> None:
    assert supported_cases() == ("render-smoke", "reach", "mirrors")

    def runner(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"unexpected runtime call: {command}")

    try:
        run_harbor(tmp_path, case="unknown", runner=runner)
    except ValueError as error:
        assert "unsupported Harbor case" in str(error)
    else:
        raise AssertionError("an unsupported case was accepted")
    assert not (tmp_path / "runs").exists()


def test_harbor_requires_build_when_image_is_absent(tmp_path: Path) -> None:
    def runner(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        assert command[:3] == ["docker", "image", "inspect"]
        return subprocess.CompletedProcess(command, 1)

    try:
        run_harbor(tmp_path, image="missing:test", runner=runner)
    except FileNotFoundError as error:
        assert "--build" in str(error)
    else:
        raise AssertionError("an absent image was accepted")


def test_harbor_surfaces_an_explicit_build_failure(tmp_path: Path) -> None:
    def runner(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        assert command[:2] == ["docker", "build"]
        return subprocess.CompletedProcess(command, 9)

    try:
        run_harbor(tmp_path, image="broken:test", build=True, runner=runner)
    except RuntimeError as error:
        assert "status 9" in str(error)
    else:
        raise AssertionError("a failed image build was accepted")
