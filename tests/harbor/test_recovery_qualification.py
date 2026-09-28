from dataclasses import FrozenInstanceError

import pytest

from entryplug_harbor.recovery_qualification import HARBOR_RESIDENT_RECOVERY_V1 as PROFILE


def test_recovery_profile_is_frozen_and_digest_locked() -> None:
    assert PROFILE.profile_id == "harbor-resident-recovery-v1"
    assert PROFILE.digest == (
        "sha256:f41f9900a7f25f13d6735b9cd1095add910bc155e3d66bd245acee6d88d7cec0"
    )
    with pytest.raises(FrozenInstanceError):
        PROFILE.repair_budget_seconds = 21.0  # type: ignore[misc]


def test_seed_changes_declared_goals_but_keeps_them_local() -> None:
    seeds = (*PROFILE.development_seeds, *PROFILE.holdout_seeds)
    assert len(PROFILE.development_seeds) == 3
    assert len(PROFILE.holdout_seeds) == 10
    assert set(PROFILE.development_seeds).isdisjoint(PROFILE.holdout_seeds)
    offsets = [PROFILE.target_offsets(seed) for seed in seeds]
    assert len(set(offsets)) == len(offsets)
    assert PROFILE.target_offsets(101) == (5.594, -4.747, 6.672, -6.865)
    for series in offsets:
        assert len(series) == 4
        for value, base in zip(series, PROFILE.base_offsets_px, strict=True):
            assert abs(value - base) <= PROFILE.maximum_target_jitter_px
        assert abs(series[1] - series[0]) > PROFILE.target_tolerance_px


@pytest.mark.parametrize("seed", [-1, 2**32, True, 1.5])
def test_recovery_seed_rejects_invalid_values(seed: object) -> None:
    with pytest.raises(ValueError, match="experiment seed"):
        PROFILE.target_offsets(seed)  # type: ignore[arg-type]
