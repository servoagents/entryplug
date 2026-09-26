from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

import pytest

from entryplug.detection import MarkerDetection
from entryplug.detector_worker import ActiveDetectorPath, DetectorWorker, WorkerUnavailable


class ApprovedTestDetector:
    detector_id = "test-marker-v1"
    interface_version = "1"

    def prepare(self) -> None:
        return None

    def detect(self, frame: object) -> MarkerDetection:
        assert frame.data == b"\x01\x02\x03" * 400
        return MarkerDetection(10.0, 11.0, 20, 8, 9, 12, 13)

    def close(self) -> None:
        return None


@dataclass(frozen=True)
class SampleFrame:
    width: int = 20
    height: int = 20
    encoding: str = "rgb8"
    step: int = 60
    data: bytes = b"\x01\x02\x03" * 400
    sequence: int = 7
    received_monotonic: float = 0.0
    stamp_sec: int = 11
    stamp_nanosec: int = 42
    sha256: str = hashlib.sha256(data).hexdigest()


def _worker(generation: int) -> DetectorWorker:
    return DetectorWorker(
        ApprovedTestDetector,
        detector_id="test-marker-v1",
        generation=generation,
        source_id="camera-a",
        lineage_id="capture-a",
    )


def test_worker_preserves_capture_identity_and_actual_process_death() -> None:
    primary = _worker(1)
    alternate = _worker(2)
    paths = ActiveDetectorPath(primary, alternate)
    try:
        frame = SampleFrame(received_monotonic=time.monotonic())
        assert paths.detect(frame).y_px == 11.0
        assert paths.last_reading is not None
        assert paths.last_reading.provenance()["frame_sha256"] == frame.sha256
        assert paths.last_reading.provenance()["lineage_id"] == "capture-a"
        assert paths.last_reading.worker_generation == 1
        primary.kill_for_evaluation()
        assert not primary.alive
        with pytest.raises(WorkerUnavailable, match="WORKER_PROCESS_LOST"):
            paths.detect(SampleFrame(received_monotonic=time.monotonic()))
        with pytest.raises(WorkerUnavailable, match="STOP_UNCONFIRMED"):
            paths.select_alternate(quiescence_confirmed=False)
        paths.select_alternate(quiescence_confirmed=True)
        assert paths.detect(SampleFrame(received_monotonic=time.monotonic())).y_px == 11.0
        assert paths.last_reading is not None
        assert paths.last_reading.worker_generation == 2
        alternate_observation = {"worker": paths.last_reading.provenance()}
        with pytest.raises(WorkerUnavailable, match="BINDING_NOT_VALIDATED"):
            paths.assert_ready_for_segment("task:servo-step-2", alternate_observation)
        paths.assert_ready_for_segment("task:replacement-check-1", alternate_observation)
        assert paths.binding_revision == 1
        assert paths.commit_binding_revision() == 2
        paths.assert_ready_for_segment("task:resumed:servo-step-1", alternate_observation)
        with pytest.raises(WorkerUnavailable, match="STALE_WORKER_GENERATION"):
            paths.assert_ready_for_segment("task:resumed:servo-step-1", {"worker": {}})
        with pytest.raises(WorkerUnavailable, match="BINDING_REVISION_INVALID"):
            paths.commit_binding_revision()
    finally:
        paths.close()


class SlowApprovedTestDetector(ApprovedTestDetector):
    def detect(self, frame: object) -> MarkerDetection:
        time.sleep(0.2)
        return super().detect(frame)


def test_late_primary_reply_cannot_become_alternate_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import entryplug.detector_worker as worker_module

    primary = DetectorWorker(
        SlowApprovedTestDetector,
        detector_id="test-marker-v1",
        generation=1,
        source_id="camera-a",
        lineage_id="capture-a",
    )
    alternate = _worker(2)
    paths = ActiveDetectorPath(primary, alternate)
    try:
        monkeypatch.setattr(worker_module, "RESPONSE_TIMEOUT_SECONDS", 0.01)
        with pytest.raises(WorkerUnavailable, match="WORKER_RESPONSE_TIMEOUT"):
            paths.detect(SampleFrame(received_monotonic=time.monotonic()))
        monkeypatch.setattr(worker_module, "RESPONSE_TIMEOUT_SECONDS", 2.0)
        paths.select_alternate(quiescence_confirmed=True)
        marker = paths.detect(SampleFrame(received_monotonic=time.monotonic()))
        assert marker.y_px == 11.0
        assert paths.last_reading is not None
        assert paths.last_reading.worker_instance == alternate.instance
        time.sleep(0.25)
        assert paths.last_reading.worker_instance == alternate.instance
    finally:
        paths.close()


def test_worker_rejects_stale_capture_without_granting_readiness() -> None:
    worker = _worker(1)
    try:
        with pytest.raises(WorkerUnavailable, match="STALE_WORKER_OBSERVATION"):
            worker.detect(SampleFrame(received_monotonic=time.monotonic() - 2))
        assert worker.alive
    finally:
        worker.close()


def test_alive_but_slow_alternate_is_not_ready() -> None:
    primary = _worker(1)
    alternate = _worker(2)
    paths = ActiveDetectorPath(primary, alternate)
    try:
        primary.kill_for_evaluation()
        alternate.set_evaluation_delay(0.5)
        assert alternate.alive
        paths.select_alternate(quiescence_confirmed=True)
        with pytest.raises(WorkerUnavailable, match="STALE_WORKER_OBSERVATION"):
            paths.detect(SampleFrame(received_monotonic=time.monotonic()))
        assert paths.binding_revision == 1
        assert paths.last_reading is None
    finally:
        paths.close()
