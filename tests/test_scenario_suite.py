"""Required lanes cannot be skipped, reused, or relabelled as acceptance."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from test_scenario_image_identity import _runner as image_runner


def runner(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "run_hybrid", image_runner())
    path = Path(__file__).resolve().parents[1] / "containers/scenarios/run_suite.py"
    spec = importlib.util.spec_from_file_location("entryplug_scenario_suite", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "RUNS", tmp_path / "runs")
    module.RUNS.mkdir()
    monkeypatch.setattr(module, "_source_digest", lambda: "current")
    return module


@pytest.mark.parametrize("offline", [False, True])
def test_failed_preflight_launches_nothing(monkeypatch, tmp_path, capsys, offline):
    module = runner(monkeypatch, tmp_path)

    def preflight(*args, **kwargs):
        raise module.ScenarioFailure("cached service is missing")

    monkeypatch.setattr(module, "preflight", preflight)
    monkeypatch.setattr(module, "run_lane", lambda *a, **kw: pytest.fail("fixture launched"))
    assert module.main(["--offline"] if offline else []) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "blocked"
    assert all(
        row == {"status": "not_run", "reason": "preflight_failed"}
        for row in report["lanes"].values()
    )


def test_acceptance_never_runs_smoke_or_builds(monkeypatch, tmp_path, capsys):
    module = runner(monkeypatch, tmp_path)
    monkeypatch.setattr(module, "preflight", lambda *a, **kw: pytest.fail("preflight ran"))
    assert module.main(["--tier", "acceptance", "--build"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["prerequisites"]
    assert report["tier"] == "acceptance"
    assert report["status"] == "blocked"


def test_failed_lane_does_not_hide_later_results(monkeypatch, tmp_path, capsys):
    module = runner(monkeypatch, tmp_path)
    monkeypatch.setattr(module, "preflight", lambda *a, **kw: {})
    calls = []

    def run(lane, *args, **kwargs):
        calls.append(lane)
        assert kwargs["client"] == "session"
        return {"status": "failed" if lane == "mqtt" else "passed"}

    monkeypatch.setattr(module, "run_lane", run)
    assert module.main([]) == 1
    report = json.loads(capsys.readouterr().out)
    assert calls == list(module.LANES)
    assert report["status"] == "failed"
    assert report["lanes"]["hybrid"]["status"] == "passed"


def test_unselected_lanes_are_not_run(monkeypatch, tmp_path, capsys):
    module = runner(monkeypatch, tmp_path)
    monkeypatch.setattr(module, "preflight", lambda *a, **kw: {})
    monkeypatch.setattr(module, "run_lane", lambda *a, **kw: {"status": "passed"})
    assert module.main(["--scenario", "mqtt", "--offline"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["selected"] == ["mqtt"]
    assert report["lanes"]["hybrid"] == {"status": "not_run", "reason": "unselected"}


@pytest.mark.parametrize(
    "case",
    [
        "passed",
        "exit_failure",
        "manifest_failure",
        "missing",
        "disagrees",
        "stale",
        "old_run",
        "image_changed",
        "path_escape",
        "source_changed",
    ],
)
def test_lane_requires_new_matching_evidence(monkeypatch, tmp_path, case):
    module = runner(monkeypatch, tmp_path)
    directory = module.RUNS / "suite"
    directory.mkdir()
    report = {
        "status": "passed",
        "run_id": "mqtt-0123456789ab",
        "source_sha256": "current",
        "image_ids": {"harbor": "sha256:a"},
    }
    identity = {"source_sha256": "current", "image_ids": {"harbor": "sha256:a"}}
    if case == "manifest_failure":
        report["status"] = "failed"
    if case == "stale":
        report["source_sha256"] = "old"
    if case == "image_changed":
        report["image_ids"] = {"harbor": "sha256:b"}
    if case == "path_escape":
        report["run_id"] = "../mqtt-0123456789ab"
    evidence = module.RUNS / report["run_id"]
    if case == "old_run":
        evidence.mkdir()

    def execute(command, stream):
        assert "--build" not in command
        evidence.mkdir(exist_ok=True)
        stream.write(json.dumps(report))
        if case != "missing":
            (evidence / "scenario.json").write_text(
                json.dumps({} if case == "disagrees" else report)
            )
        if case == "source_changed":
            monkeypatch.setattr(module, "_source_digest", lambda: "changed")
        return 1 if case == "exit_failure" else 0

    monkeypatch.setattr(module, "execute", execute)
    row = module.run_lane("mqtt", directory, identity, client="direct", fault="none")
    assert row["status"] == ("passed" if case == "passed" else "failed")
    assert json.loads((directory / "mqtt.json").read_text()) == row


@pytest.mark.parametrize(
    "build,offline,missing",
    [(False, True, True), (False, False, True), (True, False, True), (False, True, False)],
)
def test_service_preflight_never_pulls_without_build(
    monkeypatch, tmp_path, build, offline, missing
):
    module = runner(monkeypatch, tmp_path)
    commands = []
    image = "broker@sha256:" + "a" * 64
    prepared = []

    def prepare(**kwargs):
        prepared.append(kwargs)
        return {"source_sha256": "current", "image_ids": {"harbor": "sha256:h"}}

    def command(args, label, **kwargs):
        commands.append(args)
        if "--images" in args:
            return image
        if args[:3] == ["docker", "image", "inspect"]:
            if missing and not any(cmd[:2] == ["docker", "pull"] for cmd in commands):
                raise module.ScenarioFailure("missing")
            return "sha256:b"
        return "version"

    monkeypatch.setattr(module, "_prepare_images", prepare)
    monkeypatch.setattr(module, "_command", command)
    if missing and not build:
        with pytest.raises(module.ScenarioFailure, match="without --offline"):
            module.preflight(["mqtt"], build=build, offline=offline)
    else:
        assert module.preflight(["mqtt"], build=build, offline=offline)["service_image_ids"] == {
            image: "sha256:b"
        }
    assert any(cmd[:2] == ["docker", "pull"] for cmd in commands) == (missing and build)
    assert prepared == [{"build": build, "mixed": True, "native_worker": False}]


def test_offline_build_rejected_before_commands(monkeypatch, tmp_path):
    module = runner(monkeypatch, tmp_path)
    monkeypatch.setattr(module, "_command", lambda *a, **kw: pytest.fail("command ran"))
    with pytest.raises(module.ScenarioFailure, match="cannot be combined"):
        module.preflight(["mqtt"], build=True, offline=True)


def test_interrupt_gives_owned_runner_time_to_cleanup(monkeypatch, tmp_path):
    module = runner(monkeypatch, tmp_path)
    calls = []

    class Child:
        pid = 42

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def wait(self):
            calls.append("wait")
            if len(calls) == 1:
                raise KeyboardInterrupt
            return 130

        def send_signal(self, value):
            calls.append(value)

    monkeypatch.setattr(module.subprocess, "Popen", lambda *a, **kw: Child())
    with pytest.raises(KeyboardInterrupt):
        module.execute(["runner"], None)
    assert calls == ["wait", module.signal.SIGINT, "wait"]
