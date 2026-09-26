"""Frozen task variation and limits for the resident detector-loss experiment."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

from entryplug.seeding import validate_experiment_seed


@dataclass(frozen=True, slots=True)
class ResidentRecoveryProfile:
    """Declared variation and limits; the seed changes goals, not hidden physics."""

    profile_id: str
    target_rule_id: str
    base_offsets_px: tuple[float, ...]
    maximum_target_jitter_px: float
    target_tolerance_px: float
    maximum_observation_age_ms: float
    operation_deadline_seconds: float
    cancellation_grace_seconds: float
    stale_alternate_delay_seconds: float
    fault_task_index: int
    fault_phase: str
    fault_servo_step: int
    repair_budget_seconds: float
    replacement_probes_radians: tuple[float, ...]
    development_seeds: tuple[int, ...]
    holdout_seeds: tuple[int, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @property
    def digest(self) -> str:
        encoded = json.dumps(
            self.to_dict(), allow_nan=False, separators=(",", ":"), sort_keys=True
        ).encode()
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"

    def target_offsets(self, seed: int) -> tuple[float, ...]:
        """Return reproducible small image-row offsets for four local tasks."""

        checked = validate_experiment_seed(seed)
        offsets: list[float] = []
        for index, base in enumerate(self.base_offsets_px):
            digest = hashlib.sha256(
                f"{self.target_rule_id}\0{checked}\0{index}".encode("ascii")
            ).digest()
            fraction = int.from_bytes(digest[:4], "big") / (2**32 - 1)
            jitter = self.maximum_target_jitter_px * (2 * fraction - 1)
            offsets.append(round(base + jitter, 3))
        return tuple(offsets)


HARBOR_RESIDENT_RECOVERY_V1 = ResidentRecoveryProfile(
    profile_id="harbor-resident-recovery-v1",
    target_rule_id="entryplug-resident-target-jitter-v1",
    base_offsets_px=(5.0, -5.0, 7.0, -7.0),
    maximum_target_jitter_px=0.75,
    target_tolerance_px=3.0,
    maximum_observation_age_ms=250.0,
    operation_deadline_seconds=55.0,
    cancellation_grace_seconds=12.0,
    stale_alternate_delay_seconds=0.5,
    fault_task_index=2,
    fault_phase="native_correction_accepted",
    fault_servo_step=1,
    repair_budget_seconds=20.0,
    replacement_probes_radians=(0.04, -0.04),
    development_seeds=(101, 202, 303),
    holdout_seeds=(1109, 1213, 1321, 1427, 1531, 1601, 1709, 1811, 1907, 2017),
)
