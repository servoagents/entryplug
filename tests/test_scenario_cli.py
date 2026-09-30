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


def test_scenario_suite_requires_an_explicit_lane() -> None:
    with pytest.raises(SystemExit):
        cli._parse_args(["test", "--suite", "scenarios"])


def test_unimplemented_lane_fails_without_launching(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "source_root", lambda: Path("/unused"))
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("unsupported scenario must not launch"),
    )
    selected = cli._parse_args(["test", "--suite", "scenarios", "--scenario", "mqtt"])
    assert cli._test(selected) == 2


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
