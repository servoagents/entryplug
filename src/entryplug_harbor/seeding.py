"""Deterministic, domain separated seeds for experiment random streams."""

from __future__ import annotations

import hashlib

MAXIMUM_EXPERIMENT_SEED = 2**32 - 1


def validate_experiment_seed(seed: int) -> int:
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("experiment seed must be an integer")
    if seed < 0 or seed > MAXIMUM_EXPERIMENT_SEED:
        raise ValueError(f"experiment seed must be between 0 and {MAXIMUM_EXPERIMENT_SEED}")
    return seed


def derive_experiment_seed(seed: int, domain: str) -> int:
    """Derive one stable 64 bit stream seed without sharing PRNG state."""

    checked = validate_experiment_seed(seed)
    if not domain or not domain.isascii():
        raise ValueError("seed domain must be nonempty ASCII")
    digest = hashlib.sha256(f"entryplug-mirrors-v1\0{checked}\0{domain}".encode()).digest()
    return int.from_bytes(digest[:8], "big")
