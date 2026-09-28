"""Harbor-only checks for bounded stale worker frame handling."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
import reach

from entryplug_harbor.worker import WorkerUnavailable


def _frames(monkeypatch: pytest.MonkeyPatch, *, stale: set[int], count: int) -> object:
    frames = [
        SimpleNamespace(sequence=index, received_monotonic=time.monotonic())
        for index in range(1, count + 1)
    ]
    node = SimpleNamespace(frame=SimpleNamespace(sequence=0))

    def spin_once(current: object, *, timeout_sec: float) -> None:
        current.frame = frames.pop(0)

    def marker(frame: object) -> dict[str, object]:
        if frame.sequence in stale:
            raise WorkerUnavailable("STALE_WORKER_OBSERVATION")
        return {
            "x_px": 100.0,
            "y_px": float(frame.sequence),
            "frame": {"sequence": frame.sequence},
            "worker": None,
            "detector_id": "fixed-red-centroid-v1",
            "detector_interface_version": "1",
            "area_px": 30,
            "bounding_box_px": {},
        }

    monkeypatch.setattr(reach.rclpy, "ok", lambda: True)
    monkeypatch.setattr(reach.rclpy, "spin_once", spin_once)
    monkeypatch.setattr(reach, "_marker", marker)
    return node


def test_one_stale_frame_is_discarded_without_reusing_it(monkeypatch: pytest.MonkeyPatch) -> None:
    node = _frames(monkeypatch, stale={1}, count=4)
    observation = reach._fresh_observation(node)
    assert observation["stale_frame_discards"] == 1
    assert observation["first_frame_sequence"] == 2
    assert observation["last_frame_sequence"] == 4
    assert observation["y_px"] == 3.0


def test_persistent_staleness_still_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    node = _frames(monkeypatch, stale={1, 2, 3}, count=3)
    with pytest.raises(WorkerUnavailable, match="STALE_WORKER_OBSERVATION"):
        reach._fresh_observation(node)


def test_worker_death_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    node = _frames(monkeypatch, stale=set(), count=1)

    def lost(_frame: object) -> dict[str, object]:
        raise WorkerUnavailable("WORKER_PROCESS_LOST")

    monkeypatch.setattr(reach, "_marker", lost)
    with pytest.raises(WorkerUnavailable, match="WORKER_PROCESS_LOST"):
        reach._fresh_observation(node)
