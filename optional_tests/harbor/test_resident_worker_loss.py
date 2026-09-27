"""Harbor-only regression for a detector path lost outside the normal repair phase."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import resident

from entryplug.detector_worker import WorkerUnavailable


@pytest.mark.parametrize("confirmed", [True, False])
def test_unexpected_worker_loss_never_claims_visual_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, confirmed: bool
) -> None:
    monkeypatch.setattr(
        resident,
        "_wait_stationary",
        lambda _node, *, timeout: {"confirmed": confirmed},
    )
    result = resident._unavailable_task_result(
        object(),
        tmp_path,
        1,
        "accepted-operation",
        245.0,
        SimpleNamespace(evidence_key="sha256:binding"),
        SimpleNamespace(binding_revision=1),
        WorkerUnavailable("STALE_WORKER_OBSERVATION"),
    )
    saved = json.loads((tmp_path / "task-0001.json").read_text(encoding="utf-8"))
    assert result["operation_id"] == "accepted-operation"
    assert result["status"] == "failed"
    assert result["visual_capability_available"] is False
    assert result["final_y_px"] is None
    assert result["quiescence_confirmed"] is confirmed
    assert saved["evaluator_only"]["false_visual_completion"] is False
    assert saved["fault_evaluator_only"]["applied"] is False
    assert saved["recovery"]["failure_reason"] == "STALE_WORKER_OBSERVATION"


@pytest.mark.parametrize(
    ("wire", "expected"),
    [
        (b'{"version":1}\n', b'{"version":1}\n'),
        (b'{"version":1}', b""),
        (b"x" * (resident.MAX_RECORD_BYTES + 1), b"x" * (resident.MAX_RECORD_BYTES + 1)),
    ],
)
def test_private_reader_uses_bounded_raw_fd(
    monkeypatch: pytest.MonkeyPatch, wire: bytes, expected: bytes
) -> None:
    values = iter(bytes((byte,)) for byte in wire)
    monkeypatch.setattr(resident.os, "read", lambda fd, count: next(values, b""))
    assert resident._read_control_line() == expected
