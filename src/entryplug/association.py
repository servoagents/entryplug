"""Conservative scalar association for candidate observation paths."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence


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
