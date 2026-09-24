from dataclasses import FrozenInstanceError

import pytest

from entryplug.qualification import HARBOR_MIRRORS_V1


def test_harbor_mirrors_profile_is_frozen_and_versioned() -> None:
    assert HARBOR_MIRRORS_V1.profile_id == "harbor-mirrors-qualification-v1"
    assert HARBOR_MIRRORS_V1.digest.startswith("sha256:")

    with pytest.raises(FrozenInstanceError):
        HARBOR_MIRRORS_V1.delayed_path_seconds = 1.0  # type: ignore[misc]


def test_harbor_mirrors_profile_has_signed_probes_and_disjoint_seed_sets() -> None:
    for probes in (
        HARBOR_MIRRORS_V1.fit_probes_radians,
        HARBOR_MIRRORS_V1.validation_probes_radians,
        HARBOR_MIRRORS_V1.reuse_probes_radians,
        HARBOR_MIRRORS_V1.reuse_confirmation_probes_radians,
    ):
        assert min(probes) < 0 < max(probes)

    development = set(HARBOR_MIRRORS_V1.development_seeds)
    holdout = set(HARBOR_MIRRORS_V1.holdout_seeds)
    assert len(development) == 3
    assert len(holdout) == 10
    assert development.isdisjoint(holdout)


def test_harbor_mirrors_profile_digest_locks_the_complete_contract() -> None:
    assert HARBOR_MIRRORS_V1.digest == (
        "sha256:e257d16ca99eefccecd9aeedfe521a91917c449a3d8877654caeb699a13ffa01"
    )
