"""Small, strict readers for private recovery evaluation artifacts."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from pathlib import Path


def _reject_nonfinite(value: str) -> object:
    raise ValueError(f"non-finite JSON value {value}")


def read_json_object(path: Path) -> Mapping[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_nonfinite)
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} is not a JSON object")
    return value


def object_mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def finite_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def nonnegative_int(value: object) -> int:
    return value if type(value) is int and value >= 0 else 0


def read_action_purposes(path: Path) -> tuple[str, ...]:
    purposes: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line, parse_constant=_reject_nonfinite)
        if not isinstance(record, dict) or not isinstance(record.get("purpose"), str):
            raise ValueError("native action trace has an invalid purpose")
        purposes.append(record["purpose"])
    if not purposes:
        raise ValueError("native action trace is empty")
    return tuple(purposes)
