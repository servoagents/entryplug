from __future__ import annotations

import pytest

from entryplug.cli import _parse_args


def test_resident_demo_is_explicit_and_not_an_unattended_up_case() -> None:
    args = _parse_args(["resident-demo", "--runtime", "container", "--seed", "202"])
    assert args.command == "resident-demo"
    assert args.seed == 202
    with pytest.raises(SystemExit):
        _parse_args(["up", "--runtime", "container", "--case", "resident"])
