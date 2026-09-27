from __future__ import annotations

import math
from pathlib import Path

import pytest

from entryplug.recovery_records import (
    finite_float,
    nonnegative_int,
    read_action_purposes,
    read_json_object,
)


def test_readers_reject_nonfinite_json_and_invalid_trace(tmp_path: Path) -> None:
    summary = tmp_path / "summary.json"
    summary.write_text('{"elapsed_ms": NaN}', encoding="utf-8")
    with pytest.raises(ValueError, match="non-finite"):
        read_json_object(summary)

    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"purpose": "probe", "gain": Infinity}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="non-finite"):
        read_action_purposes(trace)
    trace.write_text('{"purpose": "probe"}\n', encoding="utf-8")
    assert read_action_purposes(trace) == ("probe",)


def test_numeric_reducers_exclude_invalid_values() -> None:
    assert finite_float(2) == 2.0
    assert finite_float(True) is None
    assert finite_float(math.nan) is None
    assert finite_float(math.inf) is None
    assert finite_float(10**400) is None
    assert nonnegative_int(3) == 3
    assert nonnegative_int(-3) == 0
    assert nonnegative_int(True) == 0
