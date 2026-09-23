"""Conservative scalar association for candidate observation paths."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Sequence

from entryplug.evidence import EvidenceRecord, JsonValue


VISUAL_BINDING_KIND = "visual_binding.v1"


@dataclass(frozen=True, slots=True)
class AssociationProfile:
    """Frozen acceptance limits for one local association experiment."""

    maximum_age_ms: float = 250.0
    minimum_gain_px_per_radian: float = 40.0
    minimum_signal_to_noise: float = 2.0
    validation_floor_px: float = 2.5
    noise_multiplier: float = 2.0
    minimum_shuffle_gap_px: float = 0.5


@dataclass(frozen=True, slots=True)
class CandidateEvidence:
    """Public evidence for one opaque candidate observation path."""

    candidate_id: str
    lineage_id: str
    maximum_age_ms: float
    noise_range_px: float
    fit_commands_radians: tuple[float, ...]
    fit_effects_px: tuple[float, ...]
    validation_commands_radians: tuple[float, ...]
    validation_effects_px: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class CandidateAssessment:
    """Computed association metrics and a conservative eligibility decision."""

    candidate_id: str
    lineage_id: str
    eligible: bool
    reasons: tuple[str, ...]
    gain_px_per_radian: float
    fit_rmse_px: float
    validation_max_error_px: float
    validation_tolerance_px: float
    shuffled_rmse_px: float
    signal_to_noise: float
    maximum_age_ms: float

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "lineage_id": self.lineage_id,
            "eligible": self.eligible,
            "reasons": list(self.reasons),
            "gain_px_per_radian": self.gain_px_per_radian,
            "fit_rmse_px": self.fit_rmse_px,
            "validation_max_error_px": self.validation_max_error_px,
            "validation_tolerance_px": self.validation_tolerance_px,
            "shuffled_rmse_px": self.shuffled_rmse_px,
            "signal_to_noise": self.signal_to_noise,
            "maximum_age_ms": self.maximum_age_ms,
        }


@dataclass(frozen=True, slots=True)
class CachedVisualBinding:
    """A learned local model that remains unusable until freshly checked."""

    evidence_key: str
    candidate_id: str
    lineage_id: str
    gain_px_per_radian: float
    validity_min_radians: float
    validity_max_radians: float
    noise_range_px: float
    maximum_age_ms: float
    timing_method_id: str


def _finite(values: Sequence[float], label: str) -> tuple[float, ...]:
    result = tuple(float(value) for value in values)
    if not result or not all(math.isfinite(value) for value in result):
        raise ValueError(f"{label} must contain finite values")
    return result


def _paired(
    commands: Sequence[float], effects: Sequence[float], label: str
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    checked_commands = _finite(commands, f"{label} commands")
    checked_effects = _finite(effects, f"{label} effects")
    if len(checked_commands) != len(checked_effects):
        raise ValueError(f"{label} commands and effects must have equal length")
    return checked_commands, checked_effects


def _gain(commands: Sequence[float], effects: Sequence[float]) -> float:
    denominator = sum(command * command for command in commands)
    if denominator <= 0:
        raise ValueError("association commands cannot all be zero")
    return sum(
        command * effect
        for command, effect in zip(commands, effects, strict=True)
    ) / denominator


def _rmse(predicted: Sequence[float], observed: Sequence[float]) -> float:
    if len(predicted) != len(observed):
        raise ValueError("predicted and observed values must have equal length")
    return math.sqrt(
        sum(
            (actual - estimate) ** 2
            for estimate, actual in zip(predicted, observed, strict=True)
        )
        / len(predicted)
    )


def assess_candidate(
    evidence: CandidateEvidence,
    profile: AssociationProfile = AssociationProfile(),
) -> CandidateAssessment:
    """Fit one candidate and test it without using source role labels."""

    if not evidence.candidate_id or not evidence.lineage_id:
        raise ValueError("candidate and lineage IDs must be nonempty")
    fit_commands, fit_effects = _paired(
        evidence.fit_commands_radians, evidence.fit_effects_px, "fit"
    )
    validation_commands, validation_effects = _paired(
        evidence.validation_commands_radians,
        evidence.validation_effects_px,
        "validation",
    )
    if len(fit_commands) < 3 or len(validation_commands) < 2:
        raise ValueError("association requires at least three fit and two validation probes")
    if min(fit_commands) >= 0 or max(fit_commands) <= 0:
        raise ValueError("fit probes must include both command signs")
    if min(validation_commands) >= 0 or max(validation_commands) <= 0:
        raise ValueError("validation probes must include both command signs")
    if not math.isfinite(evidence.maximum_age_ms) or evidence.maximum_age_ms < 0:
        raise ValueError("maximum age must be finite and nonnegative")
    if not math.isfinite(evidence.noise_range_px) or evidence.noise_range_px < 0:
        raise ValueError("noise range must be finite and nonnegative")

    gain = _gain(fit_commands, fit_effects)
    fit_predictions = tuple(gain * command for command in fit_commands)
    validation_predictions = tuple(gain * command for command in validation_commands)
    fit_rmse = _rmse(fit_predictions, fit_effects)
    validation_errors = tuple(
        observed - predicted
        for predicted, observed in zip(
            validation_predictions, validation_effects, strict=True
        )
    )
    maximum_validation_error = max(abs(error) for error in validation_errors)
    tolerance = max(
        profile.validation_floor_px,
        evidence.noise_range_px * profile.noise_multiplier,
    )
    shuffled = fit_effects[1:] + fit_effects[:1]
    shuffled_rmse = _rmse(fit_predictions, shuffled)
    maximum_probe = max(abs(command) for command in fit_commands + validation_commands)
    predicted_signal = abs(gain) * maximum_probe
    signal_to_noise = predicted_signal / max(evidence.noise_range_px, 0.25)

    reasons: list[str] = []
    if evidence.maximum_age_ms > profile.maximum_age_ms:
        reasons.append("stale")
    if abs(gain) < profile.minimum_gain_px_per_radian:
        reasons.append("insufficient_gain")
    if signal_to_noise < profile.minimum_signal_to_noise:
        reasons.append("signal_not_above_noise")
    if maximum_validation_error > tolerance:
        reasons.append("validation_mismatch")
    if shuffled_rmse < fit_rmse + profile.minimum_shuffle_gap_px:
        reasons.append("no_temporal_advantage")

    return CandidateAssessment(
        candidate_id=evidence.candidate_id,
        lineage_id=evidence.lineage_id,
        eligible=not reasons,
        reasons=tuple(reasons),
        gain_px_per_radian=round(gain, 6),
        fit_rmse_px=round(fit_rmse, 6),
        validation_max_error_px=round(maximum_validation_error, 6),
        validation_tolerance_px=round(tolerance, 6),
        shuffled_rmse_px=round(shuffled_rmse, 6),
        signal_to_noise=round(signal_to_noise, 6),
        maximum_age_ms=round(evidence.maximum_age_ms, 6),
    )


def select_candidate(
    evidence: Sequence[CandidateEvidence],
    profile: AssociationProfile = AssociationProfile(),
) -> dict[str, object]:
    """Select only when one independently identified lineage remains."""

    candidate_ids = [item.candidate_id for item in evidence]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise ValueError("candidate IDs must be unique")
    assessments = tuple(assess_candidate(item, profile) for item in evidence)
    eligible = tuple(item for item in assessments if item.eligible)
    lineages = {item.lineage_id for item in eligible}
    base: dict[str, object] = {
        "assessments": [item.to_dict() for item in assessments],
        "eligible_candidate_count": len(eligible),
        "eligible_independent_lineage_count": len(lineages),
    }
    if not eligible:
        return {**base, "status": "refused", "reason_code": "no_usable_source"}
    if len(lineages) != 1:
        return {**base, "status": "refused", "reason_code": "ambiguous_sources"}
    chosen = min(
        eligible,
        key=lambda item: (item.maximum_age_ms, item.validation_max_error_px),
    )
    same_lineage = [item.candidate_id for item in eligible if item.lineage_id == chosen.lineage_id]
    return {
        **base,
        "status": "selected",
        "candidate_id": chosen.candidate_id,
        "lineage_id": chosen.lineage_id,
        "same_lineage_paths": sorted(same_lineage),
        "same_lineage_paths_are_independent_evidence": False,
    }


def metadata_only_selection(
    evidence: Sequence[CandidateEvidence],
    profile: AssociationProfile = AssociationProfile(),
) -> dict[str, object]:
    """Show what freshness and lineage metadata can decide without interventions."""

    fresh = tuple(item for item in evidence if item.maximum_age_ms <= profile.maximum_age_ms)
    lineages = {item.lineage_id for item in fresh}
    result: dict[str, object] = {
        "fresh_candidate_ids": sorted(item.candidate_id for item in fresh),
        "fresh_independent_lineage_count": len(lineages),
    }
    if len(lineages) != 1:
        return {**result, "status": "refused", "reason_code": "ambiguous_sources"}
    chosen = min(fresh, key=lambda item: item.maximum_age_ms)
    return {**result, "status": "selected", "candidate_id": chosen.candidate_id}


def make_visual_binding_record(
    *,
    context: Mapping[str, JsonValue],
    candidate_id: str,
    lineage_id: str,
    gain_px_per_radian: float,
    validity_radians: tuple[float, float],
    noise_range_px: float,
    maximum_age_ms: float,
    timing_method_id: str,
    evidence_refs: Sequence[str],
    created_at: str,
) -> EvidenceRecord:
    """Encode one scalar visual binding without executable cached objects."""

    numbers = (
        gain_px_per_radian,
        validity_radians[0],
        validity_radians[1],
        noise_range_px,
        maximum_age_ms,
    )
    if not candidate_id or not lineage_id or not timing_method_id:
        raise ValueError("binding identifiers and timing method must be nonempty")
    if not all(math.isfinite(value) for value in numbers):
        raise ValueError("binding values must be finite")
    if validity_radians[0] >= 0 or validity_radians[1] <= 0:
        raise ValueError("binding validity must cover signed local commands")
    if noise_range_px < 0 or maximum_age_ms < 0:
        raise ValueError("binding noise and age limits must be nonnegative")
    return EvidenceRecord.create(
        kind=VISUAL_BINDING_KIND,
        context=context,
        payload={
            "source_id": candidate_id,
            "lineage_id": lineage_id,
            "jacobian": {
                "shape": [1, 1],
                "values": [gain_px_per_radian],
            },
            "command_unit": "radian",
            "output_unit": "pixel",
            "validity_radians": list(validity_radians),
            "noise_range_px": noise_range_px,
            "maximum_age_ms": maximum_age_ms,
            "timing_method_id": timing_method_id,
        },
        evidence_refs=evidence_refs,
        created_at=created_at,
    )


def load_visual_binding(record: EvidenceRecord) -> CachedVisualBinding:
    """Decode and validate the versioned scalar visual binding schema."""

    if record.kind != VISUAL_BINDING_KIND:
        raise ValueError(f"unsupported visual binding kind: {record.kind}")
    payload = record.payload
    expected = {
        "source_id",
        "lineage_id",
        "jacobian",
        "command_unit",
        "output_unit",
        "validity_radians",
        "noise_range_px",
        "maximum_age_ms",
        "timing_method_id",
    }
    if set(payload) != expected:
        raise ValueError("visual binding fields do not match schema")
    jacobian = payload["jacobian"]
    validity = payload["validity_radians"]
    if not isinstance(jacobian, Mapping):
        raise ValueError("visual binding Jacobian must be an object")
    if jacobian.get("shape") != (1, 1):
        raise ValueError("visual binding Jacobian must have shape [1, 1]")
    values = jacobian.get("values")
    if not isinstance(values, tuple) or len(values) != 1:
        raise ValueError("visual binding Jacobian must contain one value")
    if not isinstance(validity, tuple) or len(validity) != 2:
        raise ValueError("visual binding validity must contain two bounds")
    if payload["command_unit"] != "radian" or payload["output_unit"] != "pixel":
        raise ValueError("visual binding units are unsupported")
    source_id = payload["source_id"]
    lineage_id = payload["lineage_id"]
    timing_method_id = payload["timing_method_id"]
    if not all(
        isinstance(value, str) and value
        for value in (source_id, lineage_id, timing_method_id)
    ):
        raise ValueError("visual binding identifiers must be nonempty strings")
    numeric = (
        values[0],
        validity[0],
        validity[1],
        payload["noise_range_px"],
        payload["maximum_age_ms"],
    )
    if not all(
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        for value in numeric
    ):
        raise ValueError("visual binding values must be finite numbers")
    assert isinstance(source_id, str)
    assert isinstance(lineage_id, str)
    assert isinstance(timing_method_id, str)
    binding = CachedVisualBinding(
        evidence_key=record.key,
        candidate_id=source_id,
        lineage_id=lineage_id,
        gain_px_per_radian=float(values[0]),
        validity_min_radians=float(validity[0]),
        validity_max_radians=float(validity[1]),
        noise_range_px=float(payload["noise_range_px"]),
        maximum_age_ms=float(payload["maximum_age_ms"]),
        timing_method_id=timing_method_id,
    )
    if binding.validity_min_radians >= 0 or binding.validity_max_radians <= 0:
        raise ValueError("visual binding validity must cover signed local commands")
    if binding.noise_range_px < 0 or binding.maximum_age_ms < 0:
        raise ValueError("visual binding noise and age limits must be nonnegative")
    return binding


def validate_cached_binding(
    binding: CachedVisualBinding,
    *,
    commands_radians: Sequence[float],
    candidate_effects_px: Mapping[str, Sequence[float]],
    candidate_lineages: Mapping[str, str],
    candidate_maximum_ages_ms: Mapping[str, float],
    candidate_noise_ranges_px: Mapping[str, float],
    profile: AssociationProfile = AssociationProfile(),
) -> dict[str, object]:
    """Recheck cached geometry and reject mismatches or new ambiguity."""

    commands = _finite(commands_radians, "reuse commands")
    if len(commands) < 2 or min(commands) >= 0 or max(commands) <= 0:
        raise ValueError("reuse requires at least two signed probes")
    if (
        min(commands) < binding.validity_min_radians
        or max(commands) > binding.validity_max_radians
    ):
        raise ValueError("reuse probes exceed the cached validity region")
    candidate_ids = set(candidate_effects_px)
    if not candidate_ids or any(not candidate_id for candidate_id in candidate_ids):
        raise ValueError("reuse requires candidate observations")
    if not all(
        set(mapping) == candidate_ids
        for mapping in (
            candidate_lineages,
            candidate_maximum_ages_ms,
            candidate_noise_ranges_px,
        )
    ):
        raise ValueError("reuse candidate metadata must cover the same sources")
    if binding.candidate_id not in candidate_ids:
        return {
            "status": "refused",
            "reason_code": "cached_source_unavailable",
            "evidence_key": binding.evidence_key,
        }
    if candidate_lineages[binding.candidate_id] != binding.lineage_id:
        return {
            "status": "refused",
            "reason_code": "cached_provenance_changed",
            "evidence_key": binding.evidence_key,
        }

    predicted = tuple(binding.gain_px_per_radian * command for command in commands)
    assessments: list[dict[str, object]] = []
    matching_lineages: set[str] = set()
    matching_candidates: list[str] = []
    for candidate_id in sorted(candidate_ids):
        _, effects = _paired(commands, candidate_effects_px[candidate_id], "reuse")
        age = float(candidate_maximum_ages_ms[candidate_id])
        noise = float(candidate_noise_ranges_px[candidate_id])
        if not math.isfinite(age) or age < 0 or not math.isfinite(noise) or noise < 0:
            raise ValueError("reuse candidate age and noise must be finite and nonnegative")
        maximum_error = max(
            abs(actual - expected)
            for actual, expected in zip(effects, predicted, strict=True)
        )
        tolerance = max(profile.validation_floor_px, noise * profile.noise_multiplier)
        signal_to_noise = max(abs(value) for value in predicted) / max(noise, 0.25)
        reasons: list[str] = []
        if age > min(profile.maximum_age_ms, binding.maximum_age_ms):
            reasons.append("stale")
        if maximum_error > tolerance:
            reasons.append("model_mismatch")
        if signal_to_noise < profile.minimum_signal_to_noise:
            reasons.append("signal_not_above_noise")
        eligible = not reasons
        lineage_id = candidate_lineages[candidate_id]
        if not lineage_id:
            raise ValueError("reuse candidate lineage IDs must be nonempty")
        if eligible:
            matching_candidates.append(candidate_id)
            matching_lineages.add(lineage_id)
        assessments.append(
            {
                "candidate_id": candidate_id,
                "lineage_id": lineage_id,
                "eligible": eligible,
                "reasons": reasons,
                "maximum_error_px": round(maximum_error, 6),
                "tolerance_px": round(tolerance, 6),
                "signal_to_noise": round(signal_to_noise, 6),
                "maximum_age_ms": round(age, 6),
            }
        )

    base: dict[str, object] = {
        "evidence_key": binding.evidence_key,
        "probe_count": len(commands),
        "assessments": assessments,
        "matching_candidate_ids": matching_candidates,
        "matching_independent_lineage_count": len(matching_lineages),
    }
    selected = next(
        item for item in assessments if item["candidate_id"] == binding.candidate_id
    )
    if not selected["eligible"]:
        return {**base, "status": "refused", "reason_code": "cached_model_invalid"}
    if matching_lineages != {binding.lineage_id}:
        return {**base, "status": "refused", "reason_code": "ambiguous_sources"}
    return {
        **base,
        "status": "reused",
        "candidate_id": binding.candidate_id,
        "lineage_id": binding.lineage_id,
    }
