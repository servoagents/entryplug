from __future__ import annotations

import json
from pathlib import Path

import pytest

from entryplug.cli import _parse_args, main


def test_test_command_forwards_pytest_options() -> None:
    args = _parse_args(["test", "-q", "tests/test_cli.py"])

    assert args.pytest_args == ["-q", "tests/test_cli.py"]
    assert args.test_suite == "core"


def test_test_command_selects_optional_openenv_suite() -> None:
    args = _parse_args(["test", "--suite", "openenv", "-q"])

    assert args.test_suite == "openenv"
    assert args.pytest_args == ["-q"]


def test_test_command_selects_optional_mcp_suite() -> None:
    args = _parse_args(["test", "--suite=mcp", "-q"])

    assert args.test_suite == "mcp"
    assert args.pytest_args == ["-q"]


def test_up_accepts_an_explicit_prior_mirrors_run() -> None:
    args = _parse_args(
        [
            "up",
            "--runtime",
            "container",
            "--case",
            "mirrors",
            "--reuse-from",
            "harbor-mirrors-prior",
        ]
    )

    assert args.reuse_from == "harbor-mirrors-prior"


def test_up_accepts_a_reproducible_mirrors_seed() -> None:
    args = _parse_args(
        [
            "up",
            "--runtime",
            "container",
            "--case",
            "mirrors",
            "--seed",
            "31415",
        ]
    )

    assert args.seed == 31415


def test_pilot_selects_a_frozen_seed_suite() -> None:
    args = _parse_args(["pilot", "--runtime", "container", "--phase", "holdout", "--build"])

    assert args.phase == "holdout"
    assert args.build is True


def test_openenv_replay_requires_an_explicit_source_run() -> None:
    args = _parse_args(["openenv-replay", "--source-run", "harbor-mirrors-qualified"])

    assert args.source_run == "harbor-mirrors-qualified"


def test_openenv_harbor_requires_runtime_and_seed() -> None:
    args = _parse_args(["openenv-harbor", "--runtime", "container", "--seed", "101"])

    assert args.runtime == "container"
    assert args.seed == 101


def test_a2a_harbor_requires_runtime_and_seed() -> None:
    args = _parse_args(["a2a-harbor", "--runtime", "container", "--seed", "101"])

    assert args.runtime == "container"
    assert args.seed == 101


def test_mcp_harbor_requires_runtime_and_seed() -> None:
    args = _parse_args(["mcp-harbor", "--runtime", "container", "--seed", "101"])

    assert args.runtime == "container"
    assert args.seed == 101


def test_doctor_json_is_machine_readable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "doctor.json"

    status = main(["doctor", "--json", "--output", str(output)])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert status in {0, 1}
    assert payload["report_path"] == str(output)
    assert json.loads(output.read_text(encoding="utf-8"))["schema_version"] == 2


def test_doctor_accepts_readiness_profile(capsys: pytest.CaptureFixture[str]) -> None:
    status = main(["doctor", "--profile", "core", "--json", "--no-record"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert status == 0
    assert payload["readiness_profile"] == "core"
    assert payload["counts"]["out_of_scope"] > 0


def test_doctor_refuses_to_overwrite_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "doctor.json"
    output.write_text("preserve me", encoding="utf-8")

    status = main(["doctor", "--output", str(output)])

    captured = capsys.readouterr()
    assert status == 2
    assert "File exists" in captured.err
    assert output.read_text(encoding="utf-8") == "preserve me"


def test_test_command_selects_optional_a2a_suite() -> None:
    args = _parse_args(["test", "--suite=a2a", "-q"])
    assert args.pytest_args == ["-q"]
    assert args.test_suite == "a2a"
