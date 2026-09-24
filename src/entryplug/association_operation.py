"""Shared operation contract for association over opaque visual evidence."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict

from entryplug.association import (
    DEFAULT_ASSOCIATION_PROFILE,
    AssociationProfile,
    CandidateEvidence,
    select_candidate,
)
from entryplug.evidence import JsonValue
from entryplug.operation import (
    CapabilitySpec,
    Lifecycle,
    MotionState,
    OperationContext,
    OperationHost,
    OperationResult,
)

CAPABILITY_NAME = "associate_visual_sources"


def candidate_record(evidence: CandidateEvidence) -> dict[str, object]:
    """Return the stable public wire representation of candidate evidence."""

    return asdict(evidence)


def candidate_records(evidence: Sequence[CandidateEvidence]) -> list[dict[str, object]]:
    """Return candidate evidence ready for an operation or protocol adapter."""

    return [candidate_record(item) for item in evidence]


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


def _number_tuple(value: object, label: str) -> tuple[float, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{label} must be an array")
    return tuple(_number(item, label) for item in value)


def candidate_from_record(value: object) -> CandidateEvidence:
    """Validate one untrusted public candidate record."""

    if not isinstance(value, Mapping):
        raise ValueError("each association candidate must be an object")
    expected = {
        "candidate_id",
        "lineage_id",
        "maximum_age_ms",
        "noise_range_px",
        "fit_commands_radians",
        "fit_effects_px",
        "validation_commands_radians",
        "validation_effects_px",
    }
    if set(value) != expected:
        raise ValueError("association candidate fields do not match the schema")
    candidate_id = value["candidate_id"]
    lineage_id = value["lineage_id"]
    if not isinstance(candidate_id, str) or not candidate_id:
        raise ValueError("candidate ID must be a nonempty string")
    if not isinstance(lineage_id, str) or not lineage_id:
        raise ValueError("lineage ID must be a nonempty string")
    return CandidateEvidence(
        candidate_id=candidate_id,
        lineage_id=lineage_id,
        maximum_age_ms=_number(value["maximum_age_ms"], "maximum age"),
        noise_range_px=_number(value["noise_range_px"], "noise range"),
        fit_commands_radians=_number_tuple(value["fit_commands_radians"], "fit commands"),
        fit_effects_px=_number_tuple(value["fit_effects_px"], "fit effects"),
        validation_commands_radians=_number_tuple(
            value["validation_commands_radians"], "validation commands"
        ),
        validation_effects_px=_number_tuple(
            value["validation_effects_px"], "validation effects"
        ),
    )


def association_arguments(arguments: Mapping[str, JsonValue]) -> Mapping[str, object]:
    """Validate and normalize the public association operation arguments."""

    if set(arguments) != {"candidates"}:
        raise ValueError("association requires exactly one candidate array")
    raw_candidates = arguments["candidates"]
    if not isinstance(raw_candidates, (list, tuple)) or not raw_candidates:
        raise ValueError("association requires at least one candidate")
    candidates = tuple(candidate_from_record(item) for item in raw_candidates)
    return {"candidates": candidate_records(candidates)}


def association_host(
    profile: AssociationProfile = DEFAULT_ASSOCIATION_PROFILE,
    *,
    runtime_id: str | None = None,
) -> OperationHost:
    """Create the canonical non-motion association capability host."""

    async def associate(
        context: OperationContext,
        arguments: Mapping[str, JsonValue],
    ) -> OperationResult:
        if context.cancel_requested:
            return OperationResult(
                Lifecycle.CANCELED,
                MotionState.IDLE,
                reason_code="CANCEL_REQUESTED",
            )
        context.report("associate", MotionState.IDLE)
        raw_candidates = arguments["candidates"]
        if not isinstance(raw_candidates, (list, tuple)):
            raise ValueError("validated candidates are unavailable")
        candidates = tuple(candidate_from_record(item) for item in raw_candidates)
        result = select_candidate(candidates, profile)
        return OperationResult(Lifecycle.SUCCEEDED, MotionState.IDLE, result=result)

    return OperationHost(
        (
            CapabilitySpec(
                CAPABILITY_NAME,
                "1",
                "Select one usable opaque visual lineage from intervention evidence",
                False,
                5.0,
                0.25,
                association_arguments,
                associate,
            ),
        ),
        runtime_id=runtime_id,
    )
