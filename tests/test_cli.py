from __future__ import annotations

import json
from pathlib import Path

import pytest

from entryplug.cli import _parse_args, main


def test_test_command_forwards_pytest_options() -> None:
    args = _parse_args(["test", "-q", "tests/test_cli.py"])

    assert args.pytest_args == ["-q", "tests/test_cli.py"]


def test_doctor_json_is_machine_readable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "doctor.json"

    status = main(["doctor", "--json", "--output", str(output)])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert status in {0, 1}
    assert payload["report_path"] == str(output)
    assert json.loads(output.read_text(encoding="utf-8"))["schema_version"] == 1


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
