from __future__ import annotations

import pytest

from entryplug.seeding import derive_experiment_seed, validate_experiment_seed


def test_seed_derivation_is_stable_and_domain_separated() -> None:
    solver = derive_experiment_seed(42, "solver")

    assert solver == derive_experiment_seed(42, "solver")
    assert solver != derive_experiment_seed(42, "independent_fixture")
    assert solver != derive_experiment_seed(43, "solver")


@pytest.mark.parametrize("seed", (-1, 2**32, True, 1.5))
def test_experiment_seed_has_a_small_portable_range(seed: object) -> None:
    with pytest.raises(ValueError, match="experiment seed"):
        validate_experiment_seed(seed)  # type: ignore[arg-type]


def test_seed_domain_must_be_explicit_ascii() -> None:
    with pytest.raises(ValueError, match="domain"):
        derive_experiment_seed(1, "")
    with pytest.raises(ValueError, match="domain"):
        derive_experiment_seed(1, "visión")
