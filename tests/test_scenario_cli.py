"""The source-checkout scenario command never silently skips a selected lane."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from entryplug import cli


def test_hybrid_selection_keeps_build_explicit() -> None:
    selected = cli._parse_args(["test", "--suite", "scenarios", "--scenario", "hybrid"])
    assert selected.scenario == "hybrid"
    assert selected.build is False
    assert selected.fault == "none"
    built = cli._parse_args(["test", "--suite=scenarios", "--scenario", "hybrid", "--build"])
    assert built.build is True


def test_scenario_suite_defaults_to_all_lanes() -> None:
    assert cli._parse_args(["test", "--suite", "scenarios"]).scenario is None


def test_unknown_lane_is_refused() -> None:
    with pytest.raises(SystemExit):
        cli._parse_args(["test", "--suite", "scenarios", "--scenario", "unknown"])


def test_hybrid_dispatch_preserves_exit_status(monkeypatch: pytest.MonkeyPatch) -> None:
    root = Path("/checkout")
    calls: list[list[str]] = []
    monkeypatch.setattr(cli, "source_root", lambda: root)

    def run(command: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append(command)
        assert kwargs["cwd"] == root
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(subprocess, "run", run)
    selected = cli._parse_args(["test", "--suite", "scenarios", "--scenario", "hybrid", "--build"])
    assert cli._test(selected) == 1
    assert calls == [
        [
            cli.sys.executable,
            str(root / "containers/scenarios/run_hybrid.py"),
            "--build",
        ]
    ]


def test_hybrid_fault_is_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    root = Path("/checkout")
    calls: list[list[str]] = []
    monkeypatch.setattr(cli, "source_root", lambda: root)

    def run(command: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", run)
    selected = cli._parse_args(
        ["test", "--suite", "scenarios", "--scenario", "hybrid", "--fault", "no-native-worker"]
    )
    assert cli._test(selected) == 0
    assert calls[0][-2:] == ["--fault", "no-native-worker"]


def test_hybrid_session_client_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    root = Path("/checkout")
    calls: list[list[str]] = []
    monkeypatch.setattr(cli, "source_root", lambda: root)

    def run(command: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", run)
    selected = cli._parse_args(
        ["test", "--suite", "scenarios", "--scenario", "hybrid", "--client", "session"]
    )
    assert cli._test(selected) == 0
    assert calls[0][-2:] == ["--client", "session"]


def test_zenoh_lane_launches_its_own_native_join_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "source_root", lambda: Path("/checkout"))
    commands = []
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda cmd, **kwargs: (commands.append(cmd) or SimpleNamespace(returncode=0)),
    )
    args = cli._parse_args(["test", "--suite", "scenarios", "--scenario", "zenoh", "--build"])
    assert cli._test(args) == 0
    assert commands[0][1:] == ["/checkout/containers/scenarios/run_zenoh.py", "--build"]
    args.client = "session"
    assert cli._test(args) == 2
    assert len(commands) == 1


@pytest.mark.parametrize("lane", ["mqtt", "zenoh", "ros2-vision"])
def test_join_lane_rejects_unimplemented_faults(monkeypatch: pytest.MonkeyPatch, lane: str) -> None:
    monkeypatch.setattr(cli, "source_root", lambda: Path("/checkout"))
    commands = []
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda cmd, **kw: (commands.append(cmd) or SimpleNamespace(returncode=0)),
    )
    args = cli._parse_args(["test", "--suite", "scenarios", "--scenario", lane])
    assert cli._test(args) == 0
    assert commands[0][1] == "/checkout/containers/scenarios/run_" + lane.replace("-", "_") + ".py"
    args.fault = "no-native-worker"
    assert cli._test(args) == 2
    assert len(commands) == 1


def test_ros_recovery_dispatch_and_option_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "source_root", lambda: Path("/checkout"))
    commands = []
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda cmd, **kw: (commands.append(cmd) or SimpleNamespace(returncode=1)),
    )
    args = cli._parse_args(
        ["test", "--suite", "scenarios", "--scenario", "ros2-recovery", "--build"]
    )
    assert cli._test(args) == 1
    assert commands[0][1:] == ["/checkout/containers/scenarios/run_ros2_recovery.py", "--build"]
    args.fault = "no-native-worker"
    assert cli._test(args) == 2
    assert len(commands) == 1


@pytest.mark.parametrize(
    "options", [[], ["--offline"], ["--tier", "acceptance"], ["--scenario", "mqtt", "--json"]]
)
def test_suite_dispatch_preserves_options(monkeypatch, options):
    monkeypatch.setattr(cli, "source_root", lambda: Path("/checkout"))
    commands = []
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda cmd, **kw: commands.append(cmd) or SimpleNamespace(returncode=2),
    )
    args = cli._parse_args(["test", "--suite", "scenarios", *options])
    assert cli._test(args) == 2
    assert commands[0][1:] == ["/checkout/containers/scenarios/run_suite.py", *options]


@pytest.mark.parametrize(
    "options",
    [["--build", "--offline"], ["--client", "session"], ["--fault", "kill-active-worker"]],
)
def test_invalid_suite_options_do_not_launch(monkeypatch, options):
    monkeypatch.setattr(cli, "source_root", lambda: Path("/checkout"))
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **kw: pytest.fail("launched"))
    assert cli._test(cli._parse_args(["test", "--suite", "scenarios", *options])) == 2
