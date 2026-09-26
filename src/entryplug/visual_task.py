"""The bounded image-row goal shared by resident Harbor task adapters."""

from __future__ import annotations

import math
from collections.abc import Mapping

from entryplug.evidence import JsonValue

VISUAL_REACH = "visual_reach"
VISUAL_REACH_INPUT_SCHEMA = {
    "type": "object",
    "properties": {"target_y_px": {"type": "number", "minimum": 0, "exclusiveMaximum": 480}},
    "required": ["target_y_px"],
    "additionalProperties": False,
}
VISUAL_REACH_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "run_id": {"type": "string"},
        "source_id": {"type": "string"},
        "lineage_id": {"type": "string"},
        "target_y_px": {"type": "number"},
        "final_y_px": {"type": "number"},
        "final_error_px": {"type": "number"},
        "evidence_path": {"type": "string"},
    },
    "required": [
        "run_id",
        "source_id",
        "lineage_id",
        "target_y_px",
        "final_y_px",
        "final_error_px",
        "evidence_path",
    ],
    "additionalProperties": True,
}


def visual_reach_arguments(arguments: Mapping[str, JsonValue]) -> Mapping[str, object]:
    """Validate the view-specific target before an operation owns motion."""

    if set(arguments) != {"target_y_px"}:
        raise ValueError("visual reach requires exactly target_y_px")
    target = arguments["target_y_px"]
    if isinstance(target, bool) or not isinstance(target, (int, float)):
        raise ValueError("target_y_px must be a number")
    value = float(target)
    if not math.isfinite(value) or not 0 <= value < 480:
        raise ValueError("target_y_px must be finite and inside the 480 pixel image")
    return {"target_y_px": value}
