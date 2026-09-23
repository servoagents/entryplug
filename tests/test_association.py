from __future__ import annotations

from dataclasses import replace

import pytest

from entryplug.association import (
    CandidateEvidence,
    assess_candidate,
    load_visual_binding,
    make_visual_binding_record,
    metadata_only_selection,
    select_candidate,
    validate_cached_binding,
)


def _controlled(candidate_id: str = "source-a", lineage_id: str = "lineage-a") -> CandidateEvidence:
    return CandidateEvidence(
        candidate_id=candidate_id,
        lineage_id=lineage_id,
        maximum_age_ms=40.0,
        noise_range_px=0.8,
        fit_commands_radians=(-0.04, 0.025, -0.02, 0.05),
        fit_effects_px=(-3.25, 2.1, -1.7, 4.15),
        validation_commands_radians=(-0.045, 0.04),
        validation_effects_px=(-3.7, 3.35),
    )


def test_active_association_selects_controlled_source_and_rejects_controls() -> None:
    controlled = _controlled()
    delayed = replace(
        controlled,
        candidate_id="source-b",
        maximum_age_ms=480.0,
    )
    distractor = CandidateEvidence(
        candidate_id="source-c",
        lineage_id="lineage-c",
        maximum_age_ms=35.0,
        noise_range_px=4.0,
        fit_commands_radians=controlled.fit_commands_radians,
        fit_effects_px=(1.5, -2.0, 0.5, -1.0),
        validation_commands_radians=controlled.validation_commands_radians,
        validation_effects_px=(2.5, 1.5),
    )

    selection = select_candidate((distractor, delayed, controlled))

    assert selection["status"] == "selected"
    assert selection["candidate_id"] == controlled.candidate_id
    assessments = {item["candidate_id"]: item for item in selection["assessments"]}
    assert assessments[delayed.candidate_id]["reasons"] == ["stale"]
    assert not assessments[distractor.candidate_id]["eligible"]
    assert selection["eligible_independent_lineage_count"] == 1


def test_metadata_alone_refuses_two_fresh_independent_lineages() -> None:
    controlled = _controlled()
    unrelated = replace(
        controlled,
        candidate_id="source-z",
        lineage_id="lineage-z",
    )

    result = metadata_only_selection((controlled, unrelated))

    assert result["status"] == "refused"
    assert result["reason_code"] == "ambiguous_sources"


def test_indistinguishable_independent_sources_are_refused() -> None:
    controlled = _controlled()
    equally_useful = replace(
        controlled,
        candidate_id="source-z",
        lineage_id="lineage-z",
    )

    result = select_candidate((controlled, equally_useful))

    assert result["status"] == "refused"
    assert result["reason_code"] == "ambiguous_sources"
    assert result["eligible_independent_lineage_count"] == 2


def test_duplicate_path_does_not_count_as_independent_evidence() -> None:
    controlled = _controlled()
    duplicate = replace(controlled, candidate_id="source-copy", maximum_age_ms=60.0)

    result = select_candidate((duplicate, controlled))

    assert result["status"] == "selected"
    assert result["eligible_independent_lineage_count"] == 1
    assert result["same_lineage_paths_are_independent_evidence"] is False


def test_nonfinite_or_unsigned_probe_evidence_is_rejected() -> None:
    with pytest.raises(ValueError, match="finite"):
        assess_candidate(replace(_controlled(), fit_effects_px=(1.0, 2.0, float("nan"))))
    with pytest.raises(ValueError, match="both command signs"):
        assess_candidate(
            replace(_controlled(), fit_commands_radians=(0.01, 0.02, 0.03, 0.04))
        )


def test_empty_provenance_and_duplicate_candidate_ids_are_rejected() -> None:
    with pytest.raises(ValueError, match="nonempty"):
        assess_candidate(replace(_controlled(), lineage_id=""))
    with pytest.raises(ValueError, match="unique"):
        select_candidate((_controlled(), replace(_controlled(), lineage_id="lineage-b")))


def _binding():
    record = make_visual_binding_record(
        context={
            "task": "visual_reach",
            "model_version": "scalar-jacobian-v1",
            "fixture": "harbor-mirrors-v1",
        },
        candidate_id="source-a",
        lineage_id="lineage-a",
        gain_px_per_radian=82.0,
        validity_radians=(-0.05, 0.05),
        noise_range_px=0.8,
        maximum_age_ms=250.0,
        timing_method_id="settled-before-after-v1",
        evidence_refs=("run-1/mirrors.json",),
        created_at="2026-09-23T12:00:00+00:00",
    )
    return record, load_visual_binding(record)


def _reuse_inputs() -> dict[str, object]:
    return {
        "commands_radians": (-0.03, 0.025),
        "candidate_effects_px": {
            "source-a": (-2.5, 2.08),
            "source-b": (-2.5, 2.08),
            "source-c": (1.0, -1.5),
        },
        "candidate_lineages": {
            "source-a": "lineage-a",
            "source-b": "lineage-a",
            "source-c": "lineage-c",
        },
        "candidate_maximum_ages_ms": {
            "source-a": 20.0,
            "source-b": 480.0,
            "source-c": 15.0,
        },
        "candidate_noise_ranges_px": {
            "source-a": 0.8,
            "source-b": 0.8,
            "source-c": 3.0,
        },
    }


def test_cached_binding_round_trip_preserves_explicit_model_shape_and_units() -> None:
    record, binding = _binding()

    assert record.payload["jacobian"]["shape"] == (1, 1)
    assert record.payload["command_unit"] == "radian"
    assert binding.evidence_key == record.key
    assert binding.gain_px_per_radian == 82.0


def test_checked_reuse_accepts_one_fresh_matching_lineage() -> None:
    _, binding = _binding()

    result = validate_cached_binding(binding, **_reuse_inputs())

    assert result["status"] == "reused"
    assert result["candidate_id"] == "source-a"
    assert result["probe_count"] == 2
    assert result["matching_candidate_ids"] == ["source-a"]


def test_checked_reuse_rejects_model_mismatch_and_changed_provenance() -> None:
    _, binding = _binding()
    inputs = _reuse_inputs()
    inputs["candidate_effects_px"] = {
        **inputs["candidate_effects_px"],
        "source-a": (3.0, -2.0),
    }
    mismatch = validate_cached_binding(binding, **inputs)
    assert mismatch["status"] == "refused"
    assert mismatch["reason_code"] == "cached_model_invalid"

    inputs = _reuse_inputs()
    inputs["candidate_lineages"] = {
        **inputs["candidate_lineages"],
        "source-a": "replacement-lineage",
    }
    changed = validate_cached_binding(binding, **inputs)
    assert changed["reason_code"] == "cached_provenance_changed"


def test_checked_reuse_rejects_new_independent_match() -> None:
    _, binding = _binding()
    inputs = _reuse_inputs()
    inputs["candidate_effects_px"] = {
        **inputs["candidate_effects_px"],
        "source-c": (-2.44, 2.1),
    }
    inputs["candidate_noise_ranges_px"] = {
        **inputs["candidate_noise_ranges_px"],
        "source-c": 0.5,
    }

    result = validate_cached_binding(binding, **inputs)

    assert result["status"] == "refused"
    assert result["reason_code"] == "ambiguous_sources"
