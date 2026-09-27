from __future__ import annotations

import pytest

from entryplug.cli import _parse_args


def test_resident_demo_is_explicit_and_not_an_unattended_up_case() -> None:
    args = _parse_args(["resident-demo", "--runtime", "container", "--seed", "202"])
    assert args.command == "resident-demo"
    assert args.seed == 202
    with pytest.raises(SystemExit):
        _parse_args(["up", "--runtime", "container", "--case", "resident"])


def test_resident_pilot_exposes_only_development_phase() -> None:
    args = _parse_args(["resident-pilot", "--runtime", "container", "--phase", "development"])
    assert args.command == "resident-pilot"
    with pytest.raises(SystemExit):
        _parse_args(["resident-pilot", "--runtime", "container", "--phase", "holdout"])


def test_full_reacquisition_requires_explicit_resident_demo_switch() -> None:
    args = _parse_args(
        [
            "resident-demo",
            "--runtime",
            "container",
            "--seed",
            "303",
            "--fault",
            "kill-active-worker",
            "--recovery-strategy",
            "full_reacquisition",
        ]
    )
    assert args.recovery_strategy == "full_reacquisition"
    assert args.fault == "kill-active-worker"


def test_handwritten_baseline_is_explicit_private_command() -> None:
    args = _parse_args(
        [
            "resident-handwritten",
            "--runtime",
            "container",
            "--seed",
            "202",
            "--fault",
            "kill-active-worker",
        ]
    )
    assert args.command == "resident-handwritten"
    assert args.seed == 202
    assert args.fault == "kill-active-worker"
    with pytest.raises(SystemExit):
        _parse_args(
            [
                "resident-handwritten",
                "--runtime",
                "container",
                "--fault",
                "kill-both-workers",
            ]
        )
