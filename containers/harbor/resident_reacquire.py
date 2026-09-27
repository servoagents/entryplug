"""Same-world full identification ablation for resident detector recovery."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime

from reach import (
    FIT_PROBES_RADIANS,
    RED_MARKER_DETECTOR_ID,
    VALIDATION_PROBES_RADIANS,
    Observer,
    _fit_gain,
    _fresh_observation,
    _measure_probe,
    _no_action_noise,
    _round,
    _validate_gain,
)
from smoke import _wait_stationary

from entryplug.association import (
    CachedVisualBinding,
    CandidateEvidence,
    load_visual_binding,
    make_visual_binding_record,
    select_candidate,
)
from entryplug.detector_worker import ActiveDetectorPath, WorkerUnavailable
from entryplug.harbor_resident import SOURCE_ID, SOURCE_LINEAGE
from entryplug.recovery_qualification import HARBOR_RESIDENT_RECOVERY_V1


def _check_budget(cancel: threading.Event, end: float) -> None:
    if cancel.is_set() or time.monotonic() >= end:
        raise WorkerUnavailable("REPAIR_BUDGET_EXHAUSTED")


def reacquire_after_loss(
    node: Observer,
    anchor: dict[str, float],
    old_binding: CachedVisualBinding,
    paths: ActiveDetectorPath,
    trace: list[dict[str, object]],
    prefix: str,
    cancel: threading.Event,
    deadline_monotonic: float,
) -> tuple[dict[str, object], CachedVisualBinding]:
    """Discard the old gain, fit and validate a new one without resetting the world."""

    started = time.monotonic()
    budget_end = min(
        deadline_monotonic,
        started + HARBOR_RESIDENT_RECOVERY_V1.repair_budget_seconds,
    )
    stationary = _wait_stationary(node, timeout=3.0)
    _check_budget(cancel, budget_end)
    paths.select_alternate(quiescence_confirmed=stationary["confirmed"] is True)
    first = _fresh_observation(node)
    worker = first["worker"]
    if not isinstance(worker, dict) or (
        worker.get("source_id") != SOURCE_ID
        or worker.get("lineage_id") != SOURCE_LINEAGE
        or worker.get("detector_id") != RED_MARKER_DETECTOR_ID
        or worker.get("interface_version") != "1"
        or worker.get("worker_instance") != paths.alternate.instance
        or worker.get("worker_generation") != paths.alternate.generation
    ):
        raise WorkerUnavailable("REPLACEMENT_SOURCE_INCOMPATIBLE")
    _check_budget(cancel, budget_end)
    noise = _no_action_noise(node)

    def measure(deltas: tuple[float, ...], phase: str) -> list[dict[str, object]]:
        probes: list[dict[str, object]] = []
        for delta in deltas:
            _check_budget(cancel, budget_end)
            probes.append(
                _measure_probe(
                    node,
                    anchor,
                    delta,
                    purpose=f"{prefix}:reacquire-{phase}-{len(probes) + 1}",
                    trace=trace,
                    cancel_requested=lambda: cancel.is_set() or time.monotonic() >= budget_end,
                )
            )
        return probes

    fit = measure(FIT_PROBES_RADIANS, "fit")
    try:
        model = _fit_gain(fit)
    except RuntimeError as error:
        raise WorkerUnavailable("REACQUISITION_FIT_FAILED") from error
    held_out = measure(VALIDATION_PROBES_RADIANS, "validation")
    _check_budget(cancel, budget_end)
    gain = float(model["gain_px_per_radian"])
    validation = _validate_gain(gain, held_out, noise_range_px=float(noise["y_range_px"]))
    if validation["status"] != "passed":
        raise WorkerUnavailable("REACQUISITION_VALIDATION_FAILED")
    last = _fresh_observation(node)
    candidate = CandidateEvidence(
        candidate_id=SOURCE_ID,
        lineage_id=SOURCE_LINEAGE,
        maximum_age_ms=float(last["last_receive_age_ms"]),
        noise_range_px=float(noise["y_range_px"]),
        fit_commands_radians=tuple(float(item["requested_delta_radians"]) for item in fit),
        fit_effects_px=tuple(float(item["observed_feature_delta_px"]) for item in fit),
        validation_commands_radians=tuple(
            float(item["requested_delta_radians"]) for item in held_out
        ),
        validation_effects_px=tuple(float(item["observed_feature_delta_px"]) for item in held_out),
    )
    association = select_candidate((candidate,))
    if association.get("status") != "selected":
        raise WorkerUnavailable("REACQUISITION_ASSOCIATION_FAILED")
    record = make_visual_binding_record(
        context={
            "task": "visual_reach",
            "fixture": "harbor-resident-v1",
            "source_convention": "red-centroid-y-v1",
            "recovery_strategy": "full_reacquisition",
        },
        candidate_id=SOURCE_ID,
        lineage_id=SOURCE_LINEAGE,
        gain_px_per_radian=gain,
        validity_radians=(
            min(FIT_PROBES_RADIANS + VALIDATION_PROBES_RADIANS),
            max(FIT_PROBES_RADIANS + VALIDATION_PROBES_RADIANS),
        ),
        noise_range_px=float(noise["y_range_px"]),
        maximum_age_ms=HARBOR_RESIDENT_RECOVERY_V1.maximum_observation_age_ms,
        timing_method_id="settled-before-after-v1",
        evidence_refs=(f"{prefix}.json", f"{prefix}-trace.jsonl"),
        created_at=datetime.now(UTC).isoformat(),
    )
    _check_budget(cancel, budget_end)
    new_binding = load_visual_binding(record)
    revision = paths.commit_binding_revision()
    return (
        {
            "status": "validated",
            "strategy": "full_reacquisition",
            "binding_revision": revision,
            "old_binding_revision_invalidated": True,
            "old_binding_evidence_key": old_binding.evidence_key,
            "new_binding_evidence_key": new_binding.evidence_key,
            "new_binding_record": record.to_dict(),
            "worker": last["worker"],
            "noise": noise,
            "fit_probes": fit,
            "mapping": model,
            "validation_probes": held_out,
            "validation": validation,
            "association": association,
            "identification_probe_count": len(fit),
            "validation_probe_count": len(held_out),
            "quiescence": stationary,
            "repair_ms": _round((time.monotonic() - started) * 1000),
        },
        new_binding,
    )
