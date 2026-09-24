from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from entryplug.association import CandidateEvidence
from entryplug.association_operation import (
    CAPABILITY_NAME,
    association_host,
    candidate_from_record,
    candidate_record,
    candidate_records,
)
from entryplug.operation import Lifecycle
from entryplug.session import Session


def _controlled() -> CandidateEvidence:
    return CandidateEvidence(
        candidate_id="source-a",
        lineage_id="lineage-a",
        maximum_age_ms=35.0,
        noise_range_px=0.8,
        fit_commands_radians=(-0.04, 0.025, -0.02, 0.05),
        fit_effects_px=(-3.25, 2.1, -1.7, 4.15),
        validation_commands_radians=(-0.045, 0.04),
        validation_effects_px=(-3.7, 3.35),
    )


def test_candidate_wire_record_round_trips_and_rejects_extra_fields() -> None:
    record = candidate_record(_controlled())

    assert candidate_from_record(record) == _controlled()
    with pytest.raises(ValueError, match="schema"):
        candidate_from_record({**record, "source_role": "controlled"})


def test_shared_association_operation_selects_without_source_roles() -> None:
    controlled = _controlled()
    delayed = replace(controlled, candidate_id="source-b", maximum_age_ms=480.0)

    async def exercise() -> None:
        session = Session(association_host(runtime_id="association-test"), owns_runtime=True)
        try:
            operation = await session.act(
                CAPABILITY_NAME,
                {"candidates": candidate_records((delayed, controlled))},
                request_id="stable-association-request",
            )
            completed = await session.wait(operation.operation_id, 1.0)

            assert completed.lifecycle == Lifecycle.SUCCEEDED
            assert completed.result["status"] == "selected"
            assert completed.result["candidate_id"] == "source-a"
        finally:
            await session.close()

    asyncio.run(exercise())
