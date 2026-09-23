from __future__ import annotations

from dataclasses import replace

import pytest

from entryplug.association import (
    CandidateEvidence,
    assess_candidate,
    metadata_only_selection,
    select_candidate,
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
