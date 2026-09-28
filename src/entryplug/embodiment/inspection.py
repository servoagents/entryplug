"""A portable, arm-free target inspection task over three explicit resources.

This is the in-memory task seam. Concrete camera, worker, and lighting transports
belong outside the core and must preserve the identities carried by these records.
"""

from __future__ import annotations

import math
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from entryplug.core.evidence import JsonValue
from entryplug.core.operation import (
    CapabilitySpec,
    EffectState,
    Lifecycle,
    MotionState,
    OperationContext,
    OperationResult,
)

CAPABILITY_NAME = "inspect_target"
MAX_FRAME_BYTES = 1_048_576


@dataclass(frozen=True, slots=True)
class CameraFrame:
    """One locally received RGB frame; sequence is assigned by its source."""

    source_id: str
    lineage_id: str
    sample_id: str
    sequence: int
    received_monotonic: float
    width: int
    height: int
    rgb8: bytes


@dataclass(frozen=True, slots=True)
class MarkerMeasurement:
    """Compact detector result correlated to exactly one input frame."""

    source_id: str
    sample_id: str
    worker_id: str
    program_id: str
    centroid_x: float | None
    centroid_y: float | None
    quality: float


@dataclass(frozen=True, slots=True)
class LightReport:
    """Device-reported state, not independent visual evidence of illumination."""

    entity_id: str
    level: float | None
    available: bool


class CameraSource(Protocol):
    async def capture(self) -> CameraFrame: ...


class DetectorWorker(Protocol):
    worker_id: str
    program_id: str

    async def detect(self, frame: CameraFrame) -> MarkerMeasurement: ...


class BrightnessControl(Protocol):
    entity_id: str

    async def set_brightness(self, level: float) -> LightReport: ...


@dataclass(frozen=True, slots=True)
class InspectionConfig:
    target_id: str = "bench-marker"
    minimum_quality: float = 0.5
    brightness_levels: tuple[float, ...] = (0.25, 0.50, 0.75)
    maximum_frame_age_s: float = 0.5

    def __post_init__(self) -> None:
        if not self.target_id:
            raise ValueError("target ID must be nonempty")
        if not math.isfinite(self.minimum_quality) or not 0 < self.minimum_quality <= 1:
            raise ValueError("minimum quality must be in (0, 1]")
        if not math.isfinite(self.maximum_frame_age_s) or self.maximum_frame_age_s <= 0:
            raise ValueError("maximum frame age must be positive")
        if len(self.brightness_levels) > 3 or any(
            not math.isfinite(level) or not 0 < level <= 0.75 for level in self.brightness_levels
        ):
            raise ValueError("at most three brightness levels up to 0.75 are supported")


_DEFAULT_INSPECTION_CONFIG = InspectionConfig()


class _ObservationFault(Exception):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


def _check_frame(
    frame: CameraFrame,
    previous: CameraFrame | None,
    config: InspectionConfig,
    not_before_monotonic: float | None = None,
) -> None:
    if not isinstance(frame, CameraFrame):
        raise _ObservationFault("FRAME_INVALID")
    if not all(
        isinstance(value, str) and value
        for value in (frame.source_id, frame.lineage_id, frame.sample_id)
    ):
        raise _ObservationFault("FRAME_IDENTITY_MISSING")
    if (
        type(frame.sequence) is not int
        or type(frame.width) is not int
        or type(frame.height) is not int
        or frame.width <= 0
        or frame.height <= 0
        or not isinstance(frame.rgb8, bytes)
    ):
        raise _ObservationFault("FRAME_INVALID")
    if len(frame.rgb8) != frame.width * frame.height * 3 or len(frame.rgb8) > MAX_FRAME_BYTES:
        raise _ObservationFault("FRAME_INVALID")
    if not isinstance(frame.received_monotonic, (int, float)):
        raise _ObservationFault("FRAME_INVALID")
    age = time.monotonic() - frame.received_monotonic
    if not math.isfinite(age) or not 0 <= age <= config.maximum_frame_age_s:
        raise _ObservationFault("FRAME_STALE")
    if not_before_monotonic is not None and frame.received_monotonic <= not_before_monotonic:
        raise _ObservationFault("FRAME_BEFORE_LIGHT_REPORT")
    if previous is not None:
        if (frame.source_id, frame.lineage_id) != (
            previous.source_id,
            previous.lineage_id,
        ):
            raise _ObservationFault("SOURCE_CHANGED")
        if frame.sequence <= previous.sequence or frame.sample_id == previous.sample_id:
            raise _ObservationFault("FRAME_NOT_PROGRESSING")


def _check_measurement(
    measurement: MarkerMeasurement,
    frame: CameraFrame,
    worker: DetectorWorker,
) -> bool:
    if not isinstance(measurement, MarkerMeasurement):
        raise _ObservationFault("WORKER_REPLY_INVALID")
    if (measurement.source_id, measurement.sample_id) != (
        frame.source_id,
        frame.sample_id,
    ):
        raise _ObservationFault("WORKER_REPLY_MISMATCH")
    if (measurement.worker_id, measurement.program_id) != (
        worker.worker_id,
        worker.program_id,
    ):
        raise _ObservationFault("WORKER_REPLY_MISMATCH")
    if not isinstance(measurement.quality, (int, float)) or not (
        math.isfinite(measurement.quality) and 0 <= measurement.quality <= 1
    ):
        raise _ObservationFault("WORKER_REPLY_INVALID")
    if measurement.centroid_x is None or measurement.centroid_y is None:
        return False
    if not all(
        isinstance(v, (int, float)) and math.isfinite(v)
        for v in (measurement.centroid_x, measurement.centroid_y)
    ):
        raise _ObservationFault("WORKER_REPLY_INVALID")
    if not (
        0 <= measurement.centroid_x < frame.width and 0 <= measurement.centroid_y < frame.height
    ):
        raise _ObservationFault("WORKER_REPLY_INVALID")
    return True


async def _observe_pair(
    camera: CameraSource,
    worker: DetectorWorker,
    config: InspectionConfig,
    previous: CameraFrame | None,
    not_before_monotonic: float | None = None,
) -> tuple[CameraFrame, MarkerMeasurement, bool, list[str]]:
    samples: list[str] = []
    usable = True
    last_measurement: MarkerMeasurement | None = None
    for _ in range(2):
        frame = await camera.capture()
        _check_frame(frame, previous, config, not_before_monotonic)
        measurement = await worker.detect(frame)
        detected = _check_measurement(measurement, frame, worker)
        usable &= detected and measurement.quality >= config.minimum_quality
        samples.append(frame.sample_id)
        previous = frame
        last_measurement = measurement
    assert previous is not None and last_measurement is not None
    return previous, last_measurement, usable, samples


def inspection_spec(
    camera: CameraSource,
    worker: DetectorWorker,
    *,
    light: BrightnessControl | None = None,
    config: InspectionConfig = _DEFAULT_INSPECTION_CONFIG,
) -> CapabilitySpec:
    """Bind approved resources to one task, without a provider registry."""
    if not worker.worker_id or not worker.program_id:
        raise ValueError("worker identity and program must be nonempty")
    if light is not None and not light.entity_id:
        raise ValueError("light entity ID must be nonempty")

    def validate(arguments: Mapping[str, JsonValue]) -> Mapping[str, object]:
        if set(arguments) != {"target_id"} or arguments["target_id"] != config.target_id:
            raise ValueError("only the configured target is supported")
        return {"target_id": config.target_id}

    async def inspect(
        context: OperationContext,
        _: Mapping[str, JsonValue],
    ) -> OperationResult:
        effect = EffectState.NONE
        last_level: float | None = None
        writes = 0
        previous: CameraFrame | None = None
        not_before_monotonic: float | None = None
        context.report("observe_initial", MotionState.IDLE)

        for level in (None, *(config.brightness_levels if light is not None else ())):
            if context.cancel_requested:
                return OperationResult(
                    Lifecycle.CANCELED,
                    MotionState.IDLE,
                    effect_state=effect,
                    reason_code="CANCEL_REQUESTED",
                )
            if level is not None:
                assert light is not None
                context.report("set_brightness", MotionState.IDLE)
                context.report_effect(EffectState.REQUESTED)
                effect = EffectState.REQUESTED
                writes += 1
                try:
                    report = await light.set_brightness(level)
                except Exception as error:
                    return OperationResult(
                        Lifecycle.INDETERMINATE,
                        MotionState.IDLE,
                        result={"lighting_writes": writes, "error_type": type(error).__name__},
                        reason_code="LIGHT_COMMAND_UNCONFIRMED",
                        effect_state=EffectState.UNKNOWN,
                    )
                if (
                    report.entity_id != light.entity_id
                    or not report.available
                    or report.level is None
                    or not math.isclose(report.level, level, abs_tol=0.01)
                ):
                    return OperationResult(
                        Lifecycle.INDETERMINATE,
                        MotionState.IDLE,
                        result={"lighting_writes": writes},
                        reason_code="LIGHT_STATE_UNCONFIRMED",
                        effect_state=EffectState.UNKNOWN,
                    )
                last_level = report.level
                effect = EffectState.REPORTED
                context.report_effect(effect)
                # A queued pre-report frame cannot verify this lighting change.
                not_before_monotonic = time.monotonic()
                context.report("verify_visual", MotionState.IDLE)
            try:
                previous, measurement, usable, samples = await _observe_pair(
                    camera, worker, config, previous, not_before_monotonic
                )
            except _ObservationFault as error:
                return OperationResult(
                    Lifecycle.FAILED,
                    MotionState.IDLE,
                    result={"lighting_writes": writes},
                    reason_code=error.reason_code,
                    effect_state=effect,
                )
            except Exception as error:
                return OperationResult(
                    Lifecycle.FAILED,
                    MotionState.IDLE,
                    result={"lighting_writes": writes, "error_type": type(error).__name__},
                    reason_code="OBSERVATION_PROVIDER_FAILED",
                    effect_state=effect,
                )
            if context.cancel_requested:
                return OperationResult(
                    Lifecycle.CANCELED,
                    MotionState.IDLE,
                    effect_state=effect,
                    reason_code="CANCEL_REQUESTED",
                )
            if usable:
                # A usable marker after a write is not, by itself, proof that
                # the lamp caused the change. Live image-change qualification
                # belongs to the concrete lighting fixture.
                return OperationResult(
                    Lifecycle.SUCCEEDED,
                    MotionState.IDLE,
                    {
                        "target_id": config.target_id,
                        "source_id": previous.source_id,
                        "lineage_id": previous.lineage_id,
                        "sample_ids": samples,
                        "last_sequence": previous.sequence,
                        "centroid_px": [measurement.centroid_x, measurement.centroid_y],
                        "quality": measurement.quality,
                        "worker_id": worker.worker_id,
                        "program_id": worker.program_id,
                        "binding_revision": 1,
                        "lighting_writes": writes,
                        "last_reported_level": last_level,
                    },
                    effect_state=effect,
                )

        return OperationResult(
            Lifecycle.FAILED,
            MotionState.IDLE,
            result={"lighting_writes": writes, "last_reported_level": last_level},
            reason_code="OBSERVATION_UNAVAILABLE",
            effect_state=effect,
        )

    return CapabilitySpec(
        CAPABILITY_NAME,
        "0.dev",
        "Inspect one configured marker; may set an approved light when needed"
        if light is not None
        else "Read-only inspection of one configured marker",
        False,
        30.0,
        0.5,
        validate,
        inspect,
        input_schema={
            "type": "object",
            "properties": {"target_id": {"type": "string", "const": config.target_id}},
            "required": ["target_id"],
            "additionalProperties": False,
        },
        physical_effects=light is not None,
    )
