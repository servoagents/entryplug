from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path

from entryplug.episode import EpisodeStart
from entryplug.evidence import JsonValue
from entryplug.harbor_episode import (
    ACQUIRE_VISUAL_BINDING,
    ACQUIRE_VISUAL_BINDING_INPUT_SCHEMA,
    ACQUIRE_VISUAL_BINDING_RESULT_SCHEMA,
)
from entryplug.operation import (
    CapabilitySpec,
    Lifecycle,
    MotionState,
    OperationContext,
    OperationHost,
    OperationResult,
)
from entryplug.qualification import HARBOR_MIRRORS_V1
from entryplug.session import Session
from entryplug_a2a.live_harbor import _serve_and_drive


def test_real_http_client_discovers_and_deduplicates_harbor_capability(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    async def acquire(
        _: OperationContext,
        arguments: Mapping[str, JsonValue],
    ) -> OperationResult:
        assert not arguments
        calls.append("acquire")
        return OperationResult(
            Lifecycle.SUCCEEDED,
            MotionState.IDLE,
            {
                "run_id": "harbor-mirrors-test-00000000",
                "evidence_path": str(tmp_path / "harbor-mirrors-test-00000000"),
                "status": "passed",
                "selected_candidate_id": "candidate-controlled",
                "qualification_profile": HARBOR_MIRRORS_V1.profile_id,
                "qualification_profile_digest": HARBOR_MIRRORS_V1.digest,
            },
        )

    async def factory(seed: int) -> EpisodeStart:
        assert seed == 101
        host = OperationHost(
            (
                CapabilitySpec(
                    ACQUIRE_VISUAL_BINDING,
                    "1",
                    "Acquire one visual binding",
                    True,
                    2.0,
                    0.2,
                    lambda arguments: arguments,
                    acquire,
                    ACQUIRE_VISUAL_BINDING_INPUT_SCHEMA,
                    ACQUIRE_VISUAL_BINDING_RESULT_SCHEMA,
                ),
            ),
            runtime_id="a2a-live-runtime",
        )
        return EpisodeStart(Session(host, owns_runtime=True), {"fixture": "test"})

    transport = asyncio.run(_serve_and_drive(factory, 101))

    assert transport["deduplicated_request"] is True
    assert transport["model_decision_count"] == 0
    assert transport["agent_card"]["runtime_id"] == "a2a-live-runtime"
    assert transport["agent_card"]["skills"] == [ACQUIRE_VISUAL_BINDING]
    assert transport["operation"]["lifecycle"] == "succeeded"
    assert transport["operation"]["motion_state"] == "idle"
    assert len(transport["a2a_tasks"]) == 2
    assert calls == ["acquire"]
