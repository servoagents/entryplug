from __future__ import annotations

from entryplug.agent import Act, Stop, Wait
from entryplug.resident_demo import FourRowPolicy


def test_four_row_policy_keeps_admitted_targets_absolute() -> None:
    policy = FourRowPolicy((245.0, 235.0, 247.0, 233.0))
    first = policy.decide({"operations": []})
    assert first == Act("visual_reach", {"target_y_px": 245.0})

    waiting = policy.decide({"operations": [{"operation_id": "one", "lifecycle": "running"}]})
    assert waiting == Wait("one", 2.0)

    second = policy.decide({"operations": [{"operation_id": "one", "lifecycle": "succeeded"}]})
    assert second == Act("visual_reach", {"target_y_px": 235.0})
    assert second.arguments["target_y_px"] == 235.0


def test_four_row_policy_stops_on_failure_or_after_four_goals() -> None:
    policy = FourRowPolicy((245.0, 235.0, 247.0, 233.0))
    failed = policy.decide({"operations": [{"operation_id": "one", "lifecycle": "failed"}]})
    assert isinstance(failed, Stop)
    complete = policy.decide({"operations": [{"lifecycle": "succeeded"}] * 4})
    assert complete == Stop("four visual tasks complete")
