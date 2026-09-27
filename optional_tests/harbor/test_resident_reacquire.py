"""Harbor-only contract checks for the same-world reacquisition baseline."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest
import resident_reacquire

from entryplug.association import load_visual_binding, make_visual_binding_record
from entryplug.detector_worker import WorkerUnavailable
from entryplug.harbor_resident import SOURCE_ID, SOURCE_LINEAGE


class _Paths:
    def __init__(self) -> None:
        self.alternate = SimpleNamespace(instance="alternate-worker", generation=2)
        self.selected = False
        self.committed = False

    def select_alternate(self, *, quiescence_confirmed: bool) -> None:
        assert quiescence_confirmed
        self.selected = True

    def commit_binding_revision(self) -> int:
        assert self.selected
        self.committed = True
        return 2


def _old_binding():  # type: ignore[no-untyped-def]
    record = make_visual_binding_record(
        context={"task": "visual_reach", "fixture": "old"},
        candidate_id=SOURCE_ID,
        lineage_id=SOURCE_LINEAGE,
        gain_px_per_radian=60.0,
        validity_radians=(-0.05, 0.05),
        noise_range_px=0.3,
        maximum_age_ms=250.0,
        timing_method_id="settled-before-after-v1",
        evidence_refs=("old.json",),
        created_at="2026-09-27T00:00:00+00:00",
    )
    return load_visual_binding(record)


def test_full_reacquisition_fits_new_gain_before_committing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = {
        "source_id": SOURCE_ID,
        "lineage_id": SOURCE_LINEAGE,
        "detector_id": resident_reacquire.RED_MARKER_DETECTOR_ID,
        "interface_version": "1",
        "worker_instance": "alternate-worker",
        "worker_generation": 2,
    }
    monkeypatch.setattr(
        resident_reacquire,
        "_wait_stationary",
        lambda _node, *, timeout: {"confirmed": True},
    )
    monkeypatch.setattr(
        resident_reacquire,
        "_fresh_observation",
        lambda _node: {"worker": worker, "last_receive_age_ms": 5.0},
    )
    monkeypatch.setattr(
        resident_reacquire,
        "_no_action_noise",
        lambda _node: {"y_range_px": 0.25},
    )

    def measure(_node, _anchor, delta, *, purpose, trace, cancel_requested):  # type: ignore[no-untyped-def]
        assert not cancel_requested()
        trace.append({"purpose": purpose})
        return {
            "requested_delta_radians": delta,
            "observed_feature_delta_px": delta * 95.0,
        }

    monkeypatch.setattr(resident_reacquire, "_measure_probe", measure)
    paths = _Paths()
    trace: list[dict[str, object]] = []
    old = _old_binding()
    result, new = resident_reacquire.reacquire_after_loss(
        object(),
        {"joint1": 0.0, "joint2": 0.0},
        old,
        paths,  # type: ignore[arg-type]
        trace,
        "task-0002",
        threading.Event(),
        time.monotonic() + 30.0,
    )
    assert paths.committed
    assert result["status"] == "validated"
    assert result["identification_probe_count"] == 4
    assert result["validation_probe_count"] == 2
    assert result["old_binding_evidence_key"] == old.evidence_key
    assert new.evidence_key != old.evidence_key
    assert new.gain_px_per_radian == 95.0
    assert [item["purpose"] for item in trace] == [
        *(f"task-0002:reacquire-fit-{index}" for index in range(1, 5)),
        *(f"task-0002:reacquire-validation-{index}" for index in range(1, 3)),
    ]


def test_full_reacquisition_refuses_incompatible_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        resident_reacquire,
        "_wait_stationary",
        lambda _node, *, timeout: {"confirmed": True},
    )
    monkeypatch.setattr(
        resident_reacquire,
        "_fresh_observation",
        lambda _node: {
            "worker": {
                "source_id": "unexpected",
                "lineage_id": SOURCE_LINEAGE,
                "detector_id": resident_reacquire.RED_MARKER_DETECTOR_ID,
                "interface_version": "1",
                "worker_instance": "alternate-worker",
                "worker_generation": 2,
            }
        },
    )
    paths = _Paths()
    with pytest.raises(WorkerUnavailable, match="REPLACEMENT_SOURCE_INCOMPATIBLE"):
        resident_reacquire.reacquire_after_loss(
            object(),
            {"joint1": 0.0, "joint2": 0.0},
            _old_binding(),
            paths,  # type: ignore[arg-type]
            [],
            "task-0002",
            threading.Event(),
            time.monotonic() + 30.0,
        )
    assert paths.selected
    assert not paths.committed
