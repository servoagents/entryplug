from __future__ import annotations

from dataclasses import replace

import pytest

from entryplug.evaluation import MethodMeasurement, paired_method_comparison


def _measurement(method_id: str = "handwritten") -> MethodMeasurement:
    return MethodMeasurement(
        method_id=method_id,
        input_digest="sha256:paired-input",
        task_succeeded=True,
        selected_candidate_id="view-a",
        setup_duration_ms=40.0,
        probe_count=10,
        command_travel_radians=0.35,
        token_count=0,
    )


def test_paired_comparison_reports_equal_success_without_inventing_an_advantage() -> None:
    reference = _measurement()
    contender = replace(
        reference,
        method_id="entryplug-scripted",
        setup_duration_ms=41.5,
    )

    comparison = paired_method_comparison(reference, contender)

    assert comparison["paired_inputs"] is True
    assert comparison["comparable_task_success"] is True
    assert comparison["setup_time_delta_ms"] == 1.5
    assert comparison["contender_setup_time_advantage"] is False
    assert comparison["token_count_delta"] == 0


def test_unpaired_or_different_outcomes_are_not_comparable_successes() -> None:
    reference = _measurement()
    unpaired = replace(
        reference,
        method_id="unpaired",
        input_digest="sha256:different",
    )
    wrong_source = replace(
        reference,
        method_id="wrong-source",
        selected_candidate_id="view-b",
    )

    assert paired_method_comparison(reference, unpaired)["comparable_task_success"] is False
    assert paired_method_comparison(reference, wrong_source)["comparable_task_success"] is False


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("setup_duration_ms", float("nan"), "setup duration"),
        ("probe_count", -1, "probe count"),
        ("probe_count", 1.5, "probe count"),
        ("command_travel_radians", -0.1, "command travel"),
        ("token_count", -1, "token count"),
    ),
)
def test_measurements_reject_invalid_metrics(field: str, value: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        replace(_measurement(), **{field: value})


def test_success_requires_a_selected_candidate() -> None:
    with pytest.raises(ValueError, match="successful method"):
        replace(_measurement(), selected_candidate_id=None)


def test_comparison_requires_distinct_method_ids() -> None:
    with pytest.raises(ValueError, match="distinct IDs"):
        paired_method_comparison(_measurement(), _measurement())
