from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from entryplug.runtime import (
    harbor_container_name,
    harbor_build_command,
    harbor_run_command,
    new_harbor_run_id,
    run_harbor,
    supported_cases,
)


def test_harbor_build_is_explicit_and_pins_the_dockerfile(tmp_path: Path) -> None:
    command = harbor_build_command(tmp_path, "entryplug:test", 123, 456)

    assert command[:2] == ["docker", "build"]
    assert str(tmp_path / "containers" / "harbor" / "Dockerfile") in command
    assert "ENTRYPLUG_UID=123" in command
    assert "ENTRYPLUG_GID=456" in command


def test_harbor_identity_is_shared_and_rejects_unsafe_run_ids() -> None:
    run_id = new_harbor_run_id("mirrors", 17)

    assert run_id.startswith("harbor-mirrors-s17-")
    assert harbor_container_name(run_id) == f"entryplug-harbor-{run_id}"
    with pytest.raises(ValueError, match="unsupported characters"):
        harbor_container_name("../unowned")


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
    (cache.parent / "mirrors.json").write_text(
        json.dumps({"status": "passed"}),
        encoding="utf-8",
    )

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


def test_harbor_passes_seed_as_a_separate_mirrors_argument(tmp_path: Path) -> None:
    command = harbor_run_command(
        tmp_path,
        "entryplug:test",
        "harbor-mirrors-current",
        "mirrors",
        seed=31415,
    )

    assert command[-4:] == [
        "/workspace/entryplug/runs/harbor-mirrors-current",
        "mirrors",
        "",
        "31415",
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
    (outside / "mirrors.json").write_text(
        json.dumps({"status": "passed"}),
        encoding="utf-8",
    )
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


def test_harbor_rejects_evidence_from_a_failed_mirrors_run(tmp_path: Path) -> None:
    prior = tmp_path / "runs" / "failed-run"
    prior.mkdir(parents=True)
    (prior / "evidence-cache.private.sqlite3").touch()
    (prior / "mirrors.json").write_text(
        json.dumps({"status": "failed"}),
        encoding="utf-8",
    )

    try:
        harbor_run_command(
            tmp_path,
            "entryplug:test",
            "current",
            "mirrors",
            "failed-run",
        )
    except ValueError as error:
        assert "did not pass qualification" in str(error)
    else:
        raise AssertionError("evidence from a failed run was accepted")


def test_harbor_rejects_reuse_from_a_different_experiment_seed(tmp_path: Path) -> None:
    prior = tmp_path / "runs" / "seeded-run"
    prior.mkdir(parents=True)
    (prior / "evidence-cache.private.sqlite3").touch()
    (prior / "mirrors.json").write_text(
        json.dumps({"status": "passed", "seeds": {"experiment": 7}}),
        encoding="utf-8",
    )

    try:
        harbor_run_command(
            tmp_path,
            "entryplug:test",
            "current",
            "mirrors",
            "seeded-run",
            8,
        )
    except ValueError as error:
        assert "different experiment seed" in str(error)
    else:
        raise AssertionError("evidence from a different experiment seed was accepted")


def test_harbor_accepts_reuse_from_the_same_experiment_seed(tmp_path: Path) -> None:
    prior = tmp_path / "runs" / "seeded-run"
    prior.mkdir(parents=True)
    (prior / "evidence-cache.private.sqlite3").touch()
    (prior / "mirrors.json").write_text(
        json.dumps({"status": "passed", "seeds": {"experiment": 7}}),
        encoding="utf-8",
    )

    command = harbor_run_command(
        tmp_path,
        "entryplug:test",
        "current",
        "mirrors",
        "seeded-run",
        7,
    )

    assert command[-4:] == [
        "/workspace/entryplug/runs/current",
        "mirrors",
        "seeded-run",
        "7",
    ]


def test_harbor_rejects_seed_for_non_mirror_case_before_side_effects(
    tmp_path: Path,
) -> None:
    def runner(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"unexpected runtime call: {command}")

    try:
        run_harbor(tmp_path, case="reach", seed=1, runner=runner)
    except ValueError as error:
        assert "only supported by the mirrors case" in str(error)
    else:
        raise AssertionError("a reach experiment seed was accepted")
    assert not (tmp_path / "runs").exists()


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
