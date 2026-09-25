"""Approved marker detector selection under the runtime idle barrier."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol, cast

from entryplug.operation import AdmissionError, OperationHost

DETECTOR_INTERFACE_VERSION = "1"
_MAX_APPROVED_DETECTORS = 16


class MarkerFrame(Protocol):
    width: int
    height: int
    encoding: str
    step: int
    data: bytes


@dataclass(frozen=True, slots=True)
class MarkerDetection:
    x_px: float
    y_px: float
    area_px: int
    left_px: int
    top_px: int
    right_px: int
    bottom_px: int

    def __post_init__(self) -> None:
        if not math.isfinite(self.x_px) or not math.isfinite(self.y_px):
            raise ValueError("marker centroid must be finite")
        integer_values = (
            self.area_px,
            self.left_px,
            self.top_px,
            self.right_px,
            self.bottom_px,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) for value in integer_values):
            raise ValueError("marker area and bounds must be integers")
        if self.area_px < 1:
            raise ValueError("marker area must be positive")
        if self.left_px < 0 or self.top_px < 0:
            raise ValueError("marker bounds must be nonnegative")
        if self.right_px < self.left_px or self.bottom_px < self.top_px:
            raise ValueError("marker bounds are inverted")
        if not self.left_px <= self.x_px <= self.right_px:
            raise ValueError("marker x centroid lies outside its bounds")
        if not self.top_px <= self.y_px <= self.bottom_px:
            raise ValueError("marker y centroid lies outside its bounds")

    def to_dict(self) -> dict[str, object]:
        return {
            "x_px": self.x_px,
            "y_px": self.y_px,
            "area_px": self.area_px,
            "bounding_box_px": {
                "left": self.left_px,
                "top": self.top_px,
                "right": self.right_px,
                "bottom": self.bottom_px,
            },
        }


class MarkerDetector(Protocol):
    interface_version: str
    detector_id: str

    def prepare(self) -> None: ...

    def detect(self, frame: MarkerFrame) -> MarkerDetection: ...

    def close(self) -> None: ...


DetectorFactory = Callable[[], MarkerDetector]


class DetectorSelectionError(RuntimeError):
    """Typed detector preparation, validation, or selection failure."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class MarkerDetectorView:
    detector_id: str
    generation: int
    interface_version: str
    implementation: str
    closed: bool


@dataclass(frozen=True, slots=True)
class MarkerReading:
    detector_id: str
    generation: int
    marker: MarkerDetection

    def to_dict(self) -> dict[str, object]:
        return {
            "detector_id": self.detector_id,
            "detector_generation": self.generation,
            **self.marker.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class MarkerDetectorReplacement:
    previous_detector_id: str
    detector_id: str
    previous_generation: int
    generation: int
    validation: MarkerDetection
    retired_released: bool


def _implementation(value: object) -> str:
    value_type = type(value)
    return f"{value_type.__module__}.{value_type.__qualname__}"


def _release(value: object) -> bool:
    close = getattr(value, "close", None)
    if not callable(close):
        return False
    try:
        close()
    except Exception:
        return False
    return True


def _checked_detector(value: object, expected_id: str) -> MarkerDetector:
    if getattr(value, "interface_version", None) != DETECTOR_INTERFACE_VERSION:
        raise DetectorSelectionError(
            "INCOMPATIBLE_INTERFACE",
            f"marker detector must implement interface {DETECTOR_INTERFACE_VERSION}",
        )
    if getattr(value, "detector_id", None) != expected_id:
        raise DetectorSelectionError(
            "IDENTITY_MISMATCH",
            "marker detector identity does not match its approved selection name",
        )
    for method in ("prepare", "detect", "close"):
        if not callable(getattr(value, method, None)):
            raise DetectorSelectionError(
                "INCOMPATIBLE_INTERFACE",
                f"marker detector is missing callable {method}",
            )
    return cast(MarkerDetector, value)


class MarkerDetectorSlot:
    """One marker detector selected only from an explicit local allowlist."""

    def __init__(
        self,
        host: OperationHost,
        approved: Mapping[str, DetectorFactory],
        *,
        initial_detector_id: str,
        slot: str = "detector",
    ) -> None:
        if not slot:
            raise ValueError("detector slot must be nonempty")
        if not approved or len(approved) > _MAX_APPROVED_DETECTORS:
            raise ValueError(
                f"approved detectors must contain between 1 and {_MAX_APPROVED_DETECTORS} entries"
            )
        checked_factories: dict[str, DetectorFactory] = {}
        for detector_id, factory in approved.items():
            if not detector_id or not callable(factory):
                raise ValueError("approved detector IDs and factories must be valid")
            checked_factories[detector_id] = factory
        self._host = host
        self._approved = checked_factories
        self._slot = slot
        self._generation = host.reconfiguration_generation(slot)
        self._closed = False
        self._detector = self._prepare(initial_detector_id)

    @property
    def generation(self) -> int:
        return self._generation

    def _prepare(self, detector_id: str) -> MarkerDetector:
        factory = self._approved.get(detector_id)
        if factory is None:
            raise DetectorSelectionError(
                "UNAPPROVED_DETECTOR",
                f"marker detector {detector_id!r} is not approved",
            )
        try:
            candidate = factory()
        except Exception as error:
            raise DetectorSelectionError(
                "PREPARATION_FAILED",
                f"marker detector {detector_id!r} could not be constructed",
            ) from error
        try:
            checked = _checked_detector(candidate, detector_id)
            checked.prepare()
        except DetectorSelectionError:
            _release(candidate)
            raise
        except Exception as error:
            _release(candidate)
            raise DetectorSelectionError(
                "PREPARATION_FAILED",
                f"marker detector {detector_id!r} did not become ready",
            ) from error
        return checked

    @staticmethod
    def _detect(detector: MarkerDetector, frame: MarkerFrame) -> MarkerDetection:
        try:
            result = detector.detect(frame)
        except Exception as error:
            raise DetectorSelectionError(
                "DETECTION_FAILED",
                f"marker detector {detector.detector_id!r} failed",
            ) from error
        if not isinstance(result, MarkerDetection):
            raise DetectorSelectionError(
                "INVALID_RESULT",
                f"marker detector {detector.detector_id!r} returned an invalid result",
            )
        return result

    def _require_open(self) -> None:
        if self._closed:
            raise DetectorSelectionError("SLOT_CLOSED", "marker detector slot is closed")

    def observe(self) -> MarkerDetectorView:
        return MarkerDetectorView(
            detector_id=self._detector.detector_id,
            generation=self._generation,
            interface_version=DETECTOR_INTERFACE_VERSION,
            implementation=_implementation(self._detector),
            closed=self._closed,
        )

    def detect(self, frame: MarkerFrame) -> MarkerReading:
        self._require_open()
        return MarkerReading(
            detector_id=self._detector.detector_id,
            generation=self._generation,
            marker=self._detect(self._detector, frame),
        )

    def select(
        self,
        detector_id: str,
        *,
        expected_generation: int,
        validation_frame: MarkerFrame,
    ) -> MarkerDetectorReplacement:
        """Validate an approved detector, then commit it while the runtime is idle."""

        self._require_open()
        if detector_id == self._detector.detector_id:
            raise DetectorSelectionError(
                "ALREADY_SELECTED",
                f"marker detector {detector_id!r} is already selected",
            )
        if (
            isinstance(expected_generation, bool)
            or expected_generation != self._generation
        ):
            raise DetectorSelectionError(
                "STALE_GENERATION",
                "marker detector generation is no longer current",
            )

        candidate = self._prepare(detector_id)
        try:
            try:
                validation = self._detect(candidate, validation_frame)
            except DetectorSelectionError as error:
                raise DetectorSelectionError(
                    "VALIDATION_FAILED",
                    f"marker detector {detector_id!r} failed validation",
                ) from error

            with self._host.reconfiguration(
                self._slot,
                expected_runtime_id=self._host.runtime_id,
                expected_generation=expected_generation,
            ):
                if expected_generation != self._generation:
                    raise DetectorSelectionError(
                        "STALE_GENERATION",
                        "marker detector generation changed before commit",
                    )
                retired = self._detector
                previous_detector_id = retired.detector_id
                previous_generation = self._generation
                generation = self._host.commit_reconfiguration(
                    self._slot,
                    expected_generation=expected_generation,
                )
                self._detector = candidate
                self._generation = generation
        except (AdmissionError, DetectorSelectionError):
            _release(candidate)
            raise
        except Exception as error:
            _release(candidate)
            raise DetectorSelectionError(
                "SELECTION_FAILED",
                f"marker detector {detector_id!r} was not installed",
            ) from error

        retired_released = _release(retired)
        return MarkerDetectorReplacement(
            previous_detector_id=previous_detector_id,
            detector_id=self._detector.detector_id,
            previous_generation=previous_generation,
            generation=self._generation,
            validation=validation,
            retired_released=retired_released,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._detector.close()

