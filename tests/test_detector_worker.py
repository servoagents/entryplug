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
        assert getattr(frame, "data") == b"\x01\x02\x03" * 400
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
        assert paths.binding_revision == 1
        assert paths.commit_binding_revision() == 2
        with pytest.raises(WorkerUnavailable, match="BINDING_REVISION_INVALID"):
            paths.commit_binding_revision()
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
