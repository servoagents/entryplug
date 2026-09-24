"""Small, strict records for paired Entryplug method comparisons."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class MethodMeasurement:
    """One method measured on a named, content identical task input."""

    method_id: str
    input_digest: str
    task_succeeded: bool
    selected_candidate_id: str | None
    setup_duration_ms: float
    probe_count: int
    command_travel_radians: float
    token_count: int

    def __post_init__(self) -> None:
        if not self.method_id or not self.input_digest:
            raise ValueError("method ID and input digest must be nonempty")
        if self.task_succeeded and not self.selected_candidate_id:
            raise ValueError("a successful method must identify its selected candidate")
        if not math.isfinite(self.setup_duration_ms) or self.setup_duration_ms < 0:
            raise ValueError("setup duration must be finite and nonnegative")
        if not math.isfinite(self.command_travel_radians) or self.command_travel_radians < 0:
            raise ValueError("command travel must be finite and nonnegative")
        if (
            not isinstance(self.probe_count, int)
            or isinstance(self.probe_count, bool)
            or self.probe_count < 0
        ):
            raise ValueError("probe count must be a nonnegative integer")
        if (
            not isinstance(self.token_count, int)
            or isinstance(self.token_count, bool)
            or self.token_count < 0
        ):
            raise ValueError("token count must be a nonnegative integer")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def paired_method_comparison(
    reference: MethodMeasurement,
    contender: MethodMeasurement,
) -> dict[str, object]:
    """Compare methods only when they consumed the same recorded task input."""

    if reference.method_id == contender.method_id:
        raise ValueError("paired methods must have distinct IDs")
    paired_inputs = reference.input_digest == contender.input_digest
    same_outcome = (
        reference.task_succeeded == contender.task_succeeded
        and reference.selected_candidate_id == contender.selected_candidate_id
    )
    comparable_success = paired_inputs and same_outcome and reference.task_succeeded
    setup_delta = contender.setup_duration_ms - reference.setup_duration_ms
    probe_delta = contender.probe_count - reference.probe_count
    travel_delta = contender.command_travel_radians - reference.command_travel_radians
    return {
        "reference_method_id": reference.method_id,
        "contender_method_id": contender.method_id,
        "paired_inputs": paired_inputs,
        "same_task_outcome": same_outcome,
        "comparable_task_success": comparable_success,
        "setup_time_delta_ms": round(setup_delta, 6),
        "probe_count_delta": probe_delta,
        "command_travel_delta_radians": round(travel_delta, 6),
        "contender_setup_time_advantage": comparable_success and setup_delta < 0,
        "token_count_delta": contender.token_count - reference.token_count,
    }
