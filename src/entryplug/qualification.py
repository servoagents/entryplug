"""Versioned qualification contracts for repeatable Entryplug evaluations."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

from entryplug.association import AssociationProfile


@dataclass(frozen=True, slots=True)
class MirrorsQualificationProfile:
    """All solver limits and declared seed sets for one mirrors evaluation."""

    profile_id: str
    association: AssociationProfile
    delayed_path_seconds: float
    fit_probes_radians: tuple[float, ...]
    validation_probes_radians: tuple[float, ...]
    reuse_probes_radians: tuple[float, ...]
    reuse_confirmation_probes_radians: tuple[float, ...]
    minimum_probe_gap_seconds: float
    maximum_probe_gap_seconds: float
    development_seeds: tuple[int, ...]
    holdout_seeds: tuple[int, ...]

    def to_dict(self) -> dict[str, object]:
        """Return a stable JSON-compatible representation of the contract."""

        return asdict(self)

    @property
    def digest(self) -> str:
        """Identify every value in this contract, not only its friendly name."""

        encoded = json.dumps(
            self.to_dict(),
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


HARBOR_MIRRORS_V1 = MirrorsQualificationProfile(
    profile_id="harbor-mirrors-qualification-v1",
    association=AssociationProfile(
        maximum_age_ms=250.0,
        minimum_gain_px_per_radian=40.0,
        minimum_signal_to_noise=2.0,
        validation_floor_px=2.5,
        noise_multiplier=2.0,
        minimum_shuffle_gap_px=0.5,
    ),
    delayed_path_seconds=0.36,
    fit_probes_radians=(0.018, -0.031, 0.047, -0.022, 0.036, -0.044),
    validation_probes_radians=(0.028, -0.049, 0.041, -0.034),
    reuse_probes_radians=(-0.027, 0.023),
    reuse_confirmation_probes_radians=(0.041, -0.043),
    minimum_probe_gap_seconds=0.12,
    maximum_probe_gap_seconds=0.62,
    development_seeds=(101, 202, 303),
    holdout_seeds=(1109, 1213, 1321, 1427, 1531, 1601, 1709, 1811, 1907, 2017),
)
