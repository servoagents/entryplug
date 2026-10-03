"""Unchanged scenarios may reuse images; changed sources cannot pass as current."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


def _runner() -> ModuleType:
    script = Path(__file__).resolve().parents[1] / "containers/scenarios/run_hybrid.py"
    spec = importlib.util.spec_from_file_location("entryplug_scenario_runner", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_source_digest_changes_with_selected_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runner = _runner()
    source = tmp_path / "fixture.py"
    source.write_text("first", encoding="utf-8")
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=b"fixture.py\0"),
    )
    first = runner._source_digest()
    source.write_text("second", encoding="utf-8")
    assert runner._source_digest() != first


def test_stale_image_fails_before_fixture_start(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _runner()
    commands: list[list[str]] = []
    monkeypatch.setattr(runner, "_source_digest", lambda: "current")

    def command(args: list[str], label: str) -> str:
        commands.append(args)
        return "sha256:image" if "{{.Id}}" in args else "old-source"

    monkeypatch.setattr(runner, "_command", command)
    with pytest.raises(runner.ScenarioFailure, match="stale; rerun with --build"):
        runner._prepare_images(build=False)
    assert all(args[:3] == ["docker", "image", "inspect"] for args in commands)


def test_no_build_compose_forbids_service_pulls(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _runner()
    calls: list[list[str]] = []

    def command(args: list[str], label: str, *, environment: dict[str, str]) -> str:
        calls.append(args)
        return ""

    monkeypatch.setattr(runner, "_command", command)
    stack = runner.OwnedStack("ep-test", "run-test", pull_images=False)
    try:
        stack.compose("up", "-d", "--no-build", label="starting")
    finally:
        stack.private.rmdir()
    assert calls[0][-2:] == ["--pull", "never"]


def test_absent_worker_passes_only_on_precise_refusal(tmp_path: Path) -> None:
    runner = _runner()
    token_file = tmp_path / "private-token"
    token_file.write_text("unit-secret", encoding="utf-8")
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    operation = {
        "operation_id": "op-1",
        "lifecycle": "failed",
        "reason_code": "OBSERVATION_PROVIDER_FAILED",
        "result": {"lighting_writes": 0},
    }
    (evidence / "hybrid.json").write_text(
        json.dumps({"status": "failed", "operations": [operation]}), encoding="utf-8"
    )
    (evidence / "bridge.json").write_text(
        json.dumps({"status": "stopped", "applied_count": 0}), encoding="utf-8"
    )
    assert runner._check_unavailable_result(evidence, token_file)["task_lifecycle"] == "failed"
    (evidence / "bridge.json").write_text(
        json.dumps({"status": "stopped", "applied_count": 1}), encoding="utf-8"
    )
    with pytest.raises(runner.ScenarioFailure, match="made a write"):
        runner._check_unavailable_result(evidence, token_file)


def test_mqtt_does_not_prepare_native_worker_image(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _runner()
    calls = []
    monkeypatch.setattr(runner, "_source_digest", lambda: "current")

    def command(args: list[str], label: str) -> str:
        calls.append(args)
        return "sha256:image" if "{{.Id}}" in args else "current"

    monkeypatch.setattr(runner, "_command", command)
    result = runner._prepare_images(build=False, native_worker=False)
    assert set(result["image_ids"]) == {"entryplug-harbor:jazzy", "entryplug-harbor-mixed:jazzy"}
    assert all("entryplug-scenario-worker:dev" not in args for args in calls)
