"""Owned, bounded process path for an approved stateless marker detector.

This is a private local transport. It does not select or import detector code:
the owner supplies an already approved, spawnable factory before work starts.
"""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import struct
import time
import uuid
from dataclasses import dataclass
from multiprocessing.connection import Connection
from typing import Protocol

from entryplug.detection import (
    DETECTOR_INTERFACE_VERSION,
    DetectorFactory,
    MarkerDetection,
    MarkerFrame,
    _checked_detector,
)

MAX_FRAME_BYTES = 2_000_000
MAX_RECORD_BYTES = 4096
RESPONSE_TIMEOUT_SECONDS = 2.0
STARTUP_TIMEOUT_SECONDS = 8.0


class CaptureFrame(MarkerFrame, Protocol):
    sequence: int
    received_monotonic: float
    stamp_sec: int
    stamp_nanosec: int
    sha256: str


class WorkerUnavailable(RuntimeError):
    """The selected path cannot supply a current, attributable observation."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class WorkerReading:
    marker: MarkerDetection
    worker_instance: str
    worker_generation: int
    detector_id: str
    interface_version: str
    source_id: str
    lineage_id: str
    frame_sequence: int
    frame_sha256: str
    frame_received_monotonic: float
    frame_stamp_sec: int
    frame_stamp_nanosec: int

    def provenance(self) -> dict[str, object]:
        return {
            "worker_instance": self.worker_instance,
            "worker_generation": self.worker_generation,
            "detector_id": self.detector_id,
            "interface_version": self.interface_version,
            "source_id": self.source_id,
            "lineage_id": self.lineage_id,
            "frame_sequence": self.frame_sequence,
            "frame_sha256": self.frame_sha256,
            "frame_received_monotonic": self.frame_received_monotonic,
            "frame_stamp": {
                "sec": self.frame_stamp_sec,
                "nanosec": self.frame_stamp_nanosec,
            },
        }


@dataclass(frozen=True, slots=True)
class _PackedFrame:
    width: int
    height: int
    encoding: str
    step: int
    data: bytes


def _record_bytes(record: dict[str, object], payload: bytes = b"") -> bytes:
    header = json.dumps(record, allow_nan=False, separators=(",", ":")).encode()
    if len(header) > MAX_RECORD_BYTES or len(payload) > MAX_FRAME_BYTES:
        raise WorkerUnavailable("WORKER_RECORD_OVERSIZED")
    return struct.pack("!I", len(header)) + header + payload


def _parse_bytes(raw: bytes) -> tuple[dict[str, object], bytes]:
    if len(raw) < 4:
        raise WorkerUnavailable("WORKER_RECORD_INVALID")
    header_size = struct.unpack("!I", raw[:4])[0]
    if header_size > MAX_RECORD_BYTES or len(raw) - 4 < header_size:
        raise WorkerUnavailable("WORKER_RECORD_INVALID")
    try:
        record = json.loads(raw[4 : 4 + header_size])
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WorkerUnavailable("WORKER_RECORD_INVALID") from error
    if not isinstance(record, dict):
        raise WorkerUnavailable("WORKER_RECORD_INVALID")
    return record, raw[4 + header_size :]


def _worker_main(
    connection: Connection,
    factory: DetectorFactory,
    detector_id: str,
    instance: str,
    generation: int,
) -> None:
    detector = None
    try:
        detector = _checked_detector(factory(), detector_id)
        detector.prepare()
        connection.send_bytes(
            _record_bytes(
                {
                    "type": "ready",
                    "instance": instance,
                    "generation": generation,
                    "detector_id": detector.detector_id,
                    "interface_version": detector.interface_version,
                }
            )
        )
        delay_seconds = 0.0
        while True:
            raw = connection.recv_bytes(MAX_FRAME_BYTES + MAX_RECORD_BYTES + 4)
            request, data = _parse_bytes(raw)
            if request.get("type") == "close":
                return
            if request.get("type") == "evaluation_delay":
                seconds = request.get("seconds")
                if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
                    return
                if not 0 <= seconds <= 1.0:
                    return
                delay_seconds = float(seconds)
                connection.send_bytes(
                    _record_bytes({"type": "delay_ack", "request_id": request.get("request_id")})
                )
                continue
            if request.get("type") != "detect":
                return
            if len(data) != request.get("height", -1) * request.get("step", -1) or hashlib.sha256(
                data
            ).hexdigest() != request.get("sha256"):
                return
            frame = _PackedFrame(
                width=int(request["width"]),
                height=int(request["height"]),
                encoding=str(request["encoding"]),
                step=int(request["step"]),
                data=data,
            )
            marker = detector.detect(frame)
            if delay_seconds:
                time.sleep(delay_seconds)
            if not isinstance(marker, MarkerDetection):
                return
            connection.send_bytes(
                _record_bytes(
                    {
                        "type": "reading",
                        "request_id": request["request_id"],
                        "instance": instance,
                        "generation": generation,
                        "detector_id": detector.detector_id,
                        "interface_version": detector.interface_version,
                        "source_id": request["source_id"],
                        "lineage_id": request["lineage_id"],
                        "sequence": request["sequence"],
                        "sha256": request["sha256"],
                        "received_monotonic": request["received_monotonic"],
                        "stamp_sec": request["stamp_sec"],
                        "stamp_nanosec": request["stamp_nanosec"],
                        "marker": marker.to_dict(),
                    }
                )
            )
    except (EOFError, OSError, WorkerUnavailable):
        pass
    finally:
        if detector is not None:
            detector.close()
        connection.close()


class DetectorWorker:
    """One prepared process. A killed process is never silently restarted."""

    def __init__(
        self,
        factory: DetectorFactory,
        *,
        detector_id: str,
        generation: int,
        source_id: str,
        lineage_id: str,
        max_age_ms: float = 250.0,
    ) -> None:
        self.detector_id = detector_id
        self.interface_version = DETECTOR_INTERFACE_VERSION
        self.generation = generation
        self.source_id = source_id
        self.lineage_id = lineage_id
        self.max_age_ms = max_age_ms
        self.instance = uuid.uuid4().hex
        context = multiprocessing.get_context("spawn")
        owner, child = context.Pipe(duplex=True)
        self._connection = owner
        self._process = context.Process(
            target=_worker_main,
            args=(child, factory, detector_id, self.instance, generation),
            daemon=True,
        )
        try:
            self._process.start()
            child.close()
            ready = self._receive(STARTUP_TIMEOUT_SECONDS)
            if ready != {
                "type": "ready",
                "instance": self.instance,
                "generation": generation,
                "detector_id": detector_id,
                "interface_version": DETECTOR_INTERFACE_VERSION,
            }:
                raise WorkerUnavailable("WORKER_STARTUP_INVALID")
        except Exception:
            self.close()
            raise

    @property
    def alive(self) -> bool:
        return self._process.is_alive()

    def _receive(self, timeout_seconds: float) -> dict[str, object]:
        if not self._connection.poll(timeout_seconds):
            raise WorkerUnavailable("WORKER_RESPONSE_TIMEOUT")
        try:
            record, payload = _parse_bytes(self._connection.recv_bytes(MAX_RECORD_BYTES + 4))
        except (EOFError, OSError) as error:
            raise WorkerUnavailable("WORKER_PROCESS_LOST") from error
        if payload:
            raise WorkerUnavailable("WORKER_RECORD_INVALID")
        return record

    def detect(self, frame: CaptureFrame) -> WorkerReading:
        if not self.alive:
            raise WorkerUnavailable("WORKER_PROCESS_LOST")
        if frame.encoding != "rgb8" or frame.step != frame.width * 3:
            raise WorkerUnavailable("INCOMPATIBLE_FRAME")
        if frame.width <= 0 or frame.height <= 0 or len(frame.data) != frame.height * frame.step:
            raise WorkerUnavailable("INCOMPATIBLE_FRAME")
        if len(frame.data) > MAX_FRAME_BYTES:
            raise WorkerUnavailable("WORKER_RECORD_OVERSIZED")
        request_id = uuid.uuid4().hex
        request: dict[str, object] = {
            "type": "detect",
            "request_id": request_id,
            "source_id": self.source_id,
            "lineage_id": self.lineage_id,
            "width": frame.width,
            "height": frame.height,
            "encoding": frame.encoding,
            "step": frame.step,
            "sequence": frame.sequence,
            "sha256": frame.sha256,
            "received_monotonic": frame.received_monotonic,
            "stamp_sec": frame.stamp_sec,
            "stamp_nanosec": frame.stamp_nanosec,
        }
        try:
            self._connection.send_bytes(_record_bytes(request, frame.data))
        except (BrokenPipeError, EOFError, OSError) as error:
            raise WorkerUnavailable("WORKER_PROCESS_LOST") from error
        response = self._receive(RESPONSE_TIMEOUT_SECONDS)
        expected = {
            "type": "reading",
            "request_id": request_id,
            "instance": self.instance,
            "generation": self.generation,
            "detector_id": self.detector_id,
            "interface_version": self.interface_version,
            "source_id": self.source_id,
            "lineage_id": self.lineage_id,
            "sequence": frame.sequence,
            "sha256": frame.sha256,
            "received_monotonic": frame.received_monotonic,
            "stamp_sec": frame.stamp_sec,
            "stamp_nanosec": frame.stamp_nanosec,
        }
        if any(response.get(key) != value for key, value in expected.items()):
            raise WorkerUnavailable("WORKER_RESPONSE_MISMATCH")
        bounds = response.get("marker")
        if not isinstance(bounds, dict) or not isinstance(bounds.get("bounding_box_px"), dict):
            raise WorkerUnavailable("WORKER_RESPONSE_INVALID")
        box = bounds["bounding_box_px"]
        try:
            marker = MarkerDetection(
                x_px=float(bounds["x_px"]),
                y_px=float(bounds["y_px"]),
                area_px=bounds["area_px"],
                left_px=box["left"],
                top_px=box["top"],
                right_px=box["right"],
                bottom_px=box["bottom"],
            )
        except (KeyError, TypeError, ValueError) as error:
            raise WorkerUnavailable("WORKER_RESPONSE_INVALID") from error
        age_ms = (time.monotonic() - frame.received_monotonic) * 1000
        if age_ms < 0 or age_ms > self.max_age_ms:
            raise WorkerUnavailable("STALE_WORKER_OBSERVATION")
        return WorkerReading(
            marker,
            self.instance,
            self.generation,
            self.detector_id,
            self.interface_version,
            self.source_id,
            self.lineage_id,
            frame.sequence,
            frame.sha256,
            frame.received_monotonic,
            frame.stamp_sec,
            frame.stamp_nanosec,
        )

    def set_evaluation_delay(self, seconds: float) -> None:
        """Evaluator-only latency injection; the normal freshness fence still decides."""
        if not self.alive or not 0 <= seconds <= 1.0:
            raise WorkerUnavailable("EVALUATION_DELAY_UNAVAILABLE")
        request_id = uuid.uuid4().hex
        try:
            self._connection.send_bytes(
                _record_bytes(
                    {"type": "evaluation_delay", "request_id": request_id, "seconds": seconds}
                )
            )
        except (BrokenPipeError, EOFError, OSError) as error:
            raise WorkerUnavailable("WORKER_PROCESS_LOST") from error
        if self._receive(RESPONSE_TIMEOUT_SECONDS) != {
            "type": "delay_ack",
            "request_id": request_id,
        }:
            raise WorkerUnavailable("EVALUATION_DELAY_UNAVAILABLE")

    def kill_for_evaluation(self) -> None:
        """Evaluator-only process death; callers must not use this as a health signal."""
        self._process.kill()
        self._process.join(timeout=2.0)

    def close(self) -> None:
        if self._process.is_alive():
            try:
                self._connection.send_bytes(_record_bytes({"type": "close"}))
            except (BrokenPipeError, EOFError, OSError):
                pass
            self._process.join(timeout=1.0)
        if self._process.is_alive():
            self._process.kill()
            self._process.join(timeout=2.0)
        self._connection.close()


class ActiveDetectorPath:
    """Task-local selection of one of two prepared workers, not a runtime swap."""

    def __init__(self, primary: DetectorWorker, alternate: DetectorWorker) -> None:
        if (
            primary.detector_id != alternate.detector_id
            or primary.interface_version != alternate.interface_version
            or primary.source_id != alternate.source_id
            or primary.lineage_id != alternate.lineage_id
            or primary.generation == alternate.generation
            or primary.instance == alternate.instance
        ):
            raise ValueError("prepared detector paths are not compatible and distinct")
        self.primary = primary
        self.alternate = alternate
        self.selected = primary
        self.last_reading: WorkerReading | None = None
        self.binding_revision = 1

    @property
    def detector_id(self) -> str:
        return self.selected.detector_id

    @property
    def interface_version(self) -> str:
        return self.selected.interface_version

    def detect(self, frame: CaptureFrame) -> MarkerDetection:
        reading = self.selected.detect(frame)
        self.last_reading = reading
        return reading.marker

    def assert_ready_for_segment(self, purpose: str, observation: dict[str, object]) -> None:
        """Fence native admission to the selected, fresh worker and binding revision."""
        reading = self.last_reading
        if reading is None or not self.selected.alive:
            raise WorkerUnavailable("WORKER_PROCESS_LOST")
        if (
            reading.worker_instance != self.selected.instance
            or reading.worker_generation != self.selected.generation
            or reading.source_id != self.selected.source_id
            or reading.lineage_id != self.selected.lineage_id
            or observation.get("worker") != reading.provenance()
        ):
            raise WorkerUnavailable("STALE_WORKER_GENERATION")
        if (time.monotonic() - reading.frame_received_monotonic) * 1000 > self.selected.max_age_ms:
            raise WorkerUnavailable("STALE_WORKER_OBSERVATION")
        if (
            self.selected is self.alternate
            and self.binding_revision == 1
            and ":replacement-check-" not in purpose
        ):
            raise WorkerUnavailable("BINDING_NOT_VALIDATED")

    def select_alternate(self, *, quiescence_confirmed: bool) -> None:
        if not quiescence_confirmed:
            raise WorkerUnavailable("STOP_UNCONFIRMED")
        if self.selected is not self.primary or not self.alternate.alive:
            raise WorkerUnavailable("NO_PREPARED_ALTERNATE")
        self.selected = self.alternate
        self.last_reading = None
        # The revision is committed only after current source/model validation.

    def commit_binding_revision(self) -> int:
        if self.selected is not self.alternate or self.binding_revision != 1:
            raise WorkerUnavailable("BINDING_REVISION_INVALID")
        self.binding_revision = 2
        return self.binding_revision

    def close(self) -> None:
        self.primary.close()
        self.alternate.close()
