"""Exact-key native Zenoh transport for the approved, pure RGB marker program."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import threading
import uuid
from dataclasses import dataclass, replace
from typing import Any

import zenoh

from entryplug.embodiment.inspection import (
    MAX_FRAME_BYTES,
    CameraFrame,
    MarkerMeasurement,
)
from entryplug_harbor.red_marker import FixedRedCentroidDetector

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_WIRE_VERSION = 1
_MAX_METADATA_BYTES = 4_096
_MAX_RESULT_BYTES = 4_096


class NativeDetectorError(RuntimeError):
    """A native reply was missing, stale, malformed, or untrusted."""


def detector_key(run_id: str, worker_id: str) -> str:
    """One concrete, run-scoped execution key; never a wildcard query."""
    if _ID.fullmatch(run_id) is None or _ID.fullmatch(worker_id) is None:
        raise ValueError("run and worker IDs must be short concrete key segments")
    return f"entryplug/app/inspect/{run_id}/workers/{worker_id}/detect"


def _json_bytes(value: dict[str, object]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _object(payload: bytes, label: str, limit: int) -> dict[str, Any]:
    if not payload or len(payload) > limit:
        raise NativeDetectorError(f"{label} size is invalid")
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise NativeDetectorError(f"{label} is not JSON") from error
    if not isinstance(decoded, dict):
        raise NativeDetectorError(f"{label} must be an object")
    return decoded


@dataclass(frozen=True, slots=True)
class _RGBFrame:
    width: int
    height: int
    data: bytes
    encoding: str = "rgb8"

    @property
    def step(self) -> int:
        return self.width * 3


class NativeDetectorServer:
    """One prepared worker instance. Session lifecycle belongs to its supervisor."""

    program_id = FixedRedCentroidDetector.detector_id

    def __init__(
        self, session: zenoh.Session, *, run_id: str, worker_id: str, generation: int
    ) -> None:
        if type(generation) is not int or generation < 1:
            raise ValueError("worker generation must be positive")
        self.worker_id = worker_id
        self.generation = generation
        self.key = detector_key(run_id, worker_id)
        self._detector = FixedRedCentroidDetector()
        self._detector.prepare()
        self._busy = threading.Lock()
        self._queryable = session.declare_queryable(self.key, self._handle)

    def _handle(self, query: zenoh.Query) -> None:
        try:
            if not self._busy.acquire(blocking=False):
                query.reply_err("WORKER_BUSY")
                return
            try:
                result = self._detect_query(query)
                query.reply(self.key, _json_bytes(result), encoding="application/json")
            finally:
                self._busy.release()
        except Exception:
            # A malformed request must not leak image bytes or crash the worker.
            query.reply_err("WORKER_REQUEST_INVALID")
        finally:
            query.drop()  # type: ignore[no-untyped-call]

    def _detect_query(self, query: zenoh.Query) -> dict[str, object]:
        if str(query.key_expr) != self.key or query.payload is None or query.attachment is None:
            raise NativeDetectorError("query key or content is missing")
        rgb8 = query.payload.to_bytes()
        metadata = _object(query.attachment.to_bytes(), "request metadata", _MAX_METADATA_BYTES)
        width, height = metadata.get("width"), metadata.get("height")
        if (
            type(metadata.get("wire_version")) is not int
            or metadata["wire_version"] != _WIRE_VERSION
            or type(width) is not int
            or type(height) is not int
            or width <= 0
            or height <= 0
            or len(rgb8) != width * height * 3
            or len(rgb8) > MAX_FRAME_BYTES
        ):
            raise NativeDetectorError("frame dimensions or version are invalid")
        for field in ("request_id", "source_id", "lineage_id", "sample_id"):
            value = metadata.get(field)
            if not isinstance(value, str) or not value or len(value) > 128:
                raise NativeDetectorError("request identity is invalid")
        if metadata.get("input_sha256") != hashlib.sha256(rgb8).hexdigest():
            raise NativeDetectorError("image digest does not match")
        try:
            marker = self._detector.detect(_RGBFrame(width, height, rgb8))
        except RuntimeError as error:
            if not str(error).startswith("red marker detector found only"):
                raise
            marker = None
        return {
            "wire_version": _WIRE_VERSION,
            "request_id": metadata["request_id"],
            "source_id": metadata["source_id"],
            "lineage_id": metadata["lineage_id"],
            "sample_id": metadata["sample_id"],
            "input_sha256": metadata["input_sha256"],
            "worker_id": self.worker_id,
            "worker_generation": self.generation,
            "program_id": self.program_id,
            "centroid_x": marker.x_px if marker is not None else None,
            "centroid_y": marker.y_px if marker is not None else None,
            "area_px": marker.area_px if marker is not None else 0,
        }

    def close(self) -> None:
        self._queryable.undeclare()  # type: ignore[no-untyped-call]
        self._detector.close()


class ZenohDetectorWorker:
    """Single-job camera-side client. A timed-out job poisons this instance."""

    program_id = FixedRedCentroidDetector.detector_id

    def __init__(
        self,
        session: zenoh.Session,
        *,
        run_id: str,
        worker_id: str,
        generation: int,
        timeout_s: float = 2.0,
    ) -> None:
        if type(generation) is not int or generation < 1:
            raise ValueError("worker generation must be positive")
        if not math.isfinite(timeout_s) or timeout_s <= 0 or timeout_s > 10:
            raise ValueError("worker timeout must be in (0, 10]")
        self.worker_id = worker_id
        self.generation = generation
        self.key = detector_key(run_id, worker_id)
        self._session = session
        self._timeout_s = timeout_s
        self._lock = asyncio.Lock()
        self._poisoned = False

    def _query(self, frame: CameraFrame, request_id: str) -> MarkerMeasurement:
        digest = hashlib.sha256(frame.rgb8).hexdigest()
        metadata = _json_bytes(
            {
                "wire_version": _WIRE_VERSION,
                "request_id": request_id,
                "source_id": frame.source_id,
                "lineage_id": frame.lineage_id,
                "sample_id": frame.sample_id,
                "width": frame.width,
                "height": frame.height,
                "input_sha256": digest,
            }
        )
        if len(metadata) > _MAX_METADATA_BYTES:
            raise NativeDetectorError("request metadata is too large")
        replies = list(
            self._session.get(
                self.key,
                timeout=self._timeout_s,
                payload=frame.rgb8,
                attachment=metadata,
                target=zenoh.QueryTarget.ALL,
            )
        )
        if len(replies) != 1 or replies[0].ok is None:
            raise NativeDetectorError("expected one successful exact-key worker reply")
        sample = replies[0].ok
        if str(sample.key_expr) != self.key:
            raise NativeDetectorError("worker reply key is wrong")
        result = _object(sample.payload.to_bytes(), "worker result", _MAX_RESULT_BYTES)
        expected = {
            "wire_version": _WIRE_VERSION,
            "request_id": request_id,
            "source_id": frame.source_id,
            "lineage_id": frame.lineage_id,
            "sample_id": frame.sample_id,
            "input_sha256": digest,
            "worker_id": self.worker_id,
            "worker_generation": self.generation,
            "program_id": self.program_id,
        }
        if (
            type(result.get("wire_version")) is not int
            or type(result.get("worker_generation")) is not int
        ):
            raise NativeDetectorError("worker result identity types are invalid")
        if any(result.get(field) != value for field, value in expected.items()):
            raise NativeDetectorError("worker result identity is stale or mismatched")
        x, y, area = result.get("centroid_x"), result.get("centroid_y"), result.get("area_px")
        if type(area) is not int or area < 0 or area > frame.width * frame.height:
            raise NativeDetectorError("worker marker area is invalid")
        if area == 0:
            if x is not None or y is not None:
                raise NativeDetectorError("absent marker has coordinates")
            return MarkerMeasurement(
                frame.source_id,
                frame.sample_id,
                self.worker_id,
                self.program_id,
                None,
                None,
                0.0,
            )
        if (
            isinstance(x, bool)
            or not isinstance(x, (int, float))
            or isinstance(y, bool)
            or not isinstance(y, (int, float))
            or not math.isfinite(x)
            or not math.isfinite(y)
            or not 0 <= x < frame.width
            or not 0 <= y < frame.height
        ):
            raise NativeDetectorError("worker marker coordinates are invalid")
        return MarkerMeasurement(
            frame.source_id,
            frame.sample_id,
            self.worker_id,
            self.program_id,
            float(x),
            float(y),
            min(area / 1_000, 1.0),
        )

    async def detect(self, frame: CameraFrame) -> MarkerMeasurement:
        if (
            not isinstance(frame, CameraFrame)
            or type(frame.width) is not int
            or type(frame.height) is not int
            or frame.width <= 0
            or frame.height <= 0
            or not isinstance(frame.rgb8, bytes)
            or len(frame.rgb8) != frame.width * frame.height * 3
            or len(frame.rgb8) > MAX_FRAME_BYTES
        ):
            raise NativeDetectorError("frame is invalid")
        async with self._lock:
            if self._poisoned:
                raise NativeDetectorError("worker instance requires explicit replacement")
            request_id = uuid.uuid4().hex
            try:
                measurement = await asyncio.wait_for(
                    asyncio.to_thread(self._query, frame, request_id), self._timeout_s + 0.5
                )
                return replace(
                    measurement,
                    worker_generation=self.generation,
                    job_id=request_id,
                    input_sha256=hashlib.sha256(frame.rgb8).hexdigest(),
                )
            except asyncio.CancelledError:
                self._poisoned = True
                raise
            except Exception as error:
                # The pure job may finish after a transport failure. Never
                # accept its late result or start another job on this instance.
                self._poisoned = True
                if isinstance(error, NativeDetectorError):
                    raise
                raise NativeDetectorError("native worker query failed") from error

    def close(self) -> None:
        self._poisoned = True
