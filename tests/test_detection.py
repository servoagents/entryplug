from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass

import pytest

from entryplug.detection import (
    DETECTOR_INTERFACE_VERSION,
    DetectorSelectionError,
    MarkerDetection,
    MarkerDetectorSlot,
)
from entryplug.evidence import JsonValue
from entryplug.operation import (
    AdmissionError,
    CapabilitySpec,
    Lifecycle,
    MotionState,
    OperationContext,
    OperationHost,
    OperationResult,
)


@dataclass(frozen=True)
class FixtureFrame:
    marker_x: int
    width: int = 32
    height: int = 24
    encoding: str = "rgb8"
    step: int = 96
    data: bytes = b""


class FixtureDetector:
    interface_version = DETECTOR_INTERFACE_VERSION

    def __init__(
        self,
        detector_id: str,
        *,
        offset: int = 0,
        fail_detection: bool = False,
    ) -> None:
        self.detector_id = detector_id
        self.offset = offset
        self.fail_detection = fail_detection
        self.prepared = False
        self.closed = False

    def prepare(self) -> None:
        self.prepared = True

    def detect(self, frame: FixtureFrame) -> MarkerDetection:
        if self.closed:
            raise RuntimeError("detector is closed")
        if not self.prepared:
            raise RuntimeError("detector is not prepared")
        if self.fail_detection:
            raise RuntimeError("fixture validation failure")
        x_px = frame.marker_x + self.offset
        return MarkerDetection(
            x_px=float(x_px),
            y_px=8.0,
            area_px=25,
            left_px=x_px - 2,
            top_px=6,
            right_px=x_px + 2,
            bottom_px=10,
        )

    def close(self) -> None:
        self.closed = True


def _factory(
    created: list[FixtureDetector],
    detector_id: str,
    *,
    offset: int = 0,
    fail_detection: bool = False,
):
    def create() -> FixtureDetector:
        detector = FixtureDetector(
            detector_id,
            offset=offset,
            fail_detection=fail_detection,
        )
        created.append(detector)
        return detector

    return create


def test_approved_detector_is_validated_and_committed_while_idle() -> None:
    created: list[FixtureDetector] = []
    host = OperationHost((), runtime_id="runtime-a")
    slot = MarkerDetectorSlot(
        host,
        {
            "baseline": _factory(created, "baseline"),
            "shifted": _factory(created, "shifted", offset=3),
        },
        initial_detector_id="baseline",
    )
    frame = FixtureFrame(marker_x=10)

    initial = slot.detect(frame)
    replacement = slot.select(
        "shifted",
        expected_generation=1,
        validation_frame=frame,
    )
    current = slot.detect(frame)

    assert initial.detector_id == "baseline"
    assert initial.generation == 1
    assert initial.marker.x_px == 10.0
    assert replacement.previous_detector_id == "baseline"
    assert replacement.detector_id == "shifted"
    assert replacement.previous_generation == 1
    assert replacement.generation == 2
    assert replacement.validation.x_px == 13.0
    assert replacement.retired_released
    assert created[0].closed
    assert current.detector_id == "shifted"
    assert current.generation == 2
    assert current.marker.x_px == 13.0
    assert current.to_dict()["detector_generation"] == 2
    assert host.observe().reconfiguration_generations == {"detector": 2}
    slot.close()


def test_unapproved_and_stale_detector_selections_do_not_construct_candidates() -> None:
    created: list[FixtureDetector] = []
    slot = MarkerDetectorSlot(
        OperationHost(()),
        {
            "baseline": _factory(created, "baseline"),
            "shifted": _factory(created, "shifted", offset=3),
        },
        initial_detector_id="baseline",
    )

    with pytest.raises(DetectorSelectionError) as unapproved:
        slot.select(
            "downloaded.module.Detector",
            expected_generation=1,
            validation_frame=FixtureFrame(marker_x=10),
        )
    assert unapproved.value.reason_code == "UNAPPROVED_DETECTOR"

    with pytest.raises(DetectorSelectionError) as stale:
        slot.select(
            "shifted",
            expected_generation=0,
            validation_frame=FixtureFrame(marker_x=10),
        )
    assert stale.value.reason_code == "STALE_GENERATION"
    assert len(created) == 1
    assert slot.observe().detector_id == "baseline"
    slot.close()


def test_failed_detector_validation_retains_working_detector() -> None:
    created: list[FixtureDetector] = []
    host = OperationHost(())
    slot = MarkerDetectorSlot(
        host,
        {
            "baseline": _factory(created, "baseline"),
            "broken": _factory(created, "broken", fail_detection=True),
        },
        initial_detector_id="baseline",
    )
    frame = FixtureFrame(marker_x=10)

    with pytest.raises(DetectorSelectionError) as failed:
        slot.select("broken", expected_generation=1, validation_frame=frame)

    assert failed.value.reason_code == "VALIDATION_FAILED"
    assert created[-1].closed
    assert slot.generation == 1
    assert slot.detect(frame).detector_id == "baseline"
    assert host.reconfiguration_generation("detector") == 1
    slot.close()


def test_active_operation_refuses_detector_selection_and_releases_candidate() -> None:
    async def scenario() -> None:
        release = asyncio.Event()

        def validate(arguments: Mapping[str, JsonValue]) -> Mapping[str, object]:
            return dict(arguments)

        async def read(
            _: OperationContext, __: Mapping[str, JsonValue]
        ) -> OperationResult:
            await release.wait()
            return OperationResult(Lifecycle.SUCCEEDED, MotionState.IDLE)

        host = OperationHost(
            (CapabilitySpec("read", "1", "Read", False, 1.0, 0.1, validate, read),),
            runtime_id="runtime-a",
        )
        created: list[FixtureDetector] = []
        slot = MarkerDetectorSlot(
            host,
            {
                "baseline": _factory(created, "baseline"),
                "shifted": _factory(created, "shifted", offset=3),
            },
            initial_detector_id="baseline",
        )
        operation = await host.start(
            "read",
            {},
            request_id="active",
            expected_runtime_id="runtime-a",
        )

        with pytest.raises(AdmissionError) as busy:
            slot.select(
                "shifted",
                expected_generation=1,
                validation_frame=FixtureFrame(marker_x=10),
            )

        assert busy.value.reason_code == "BUSY"
        assert created[-1].closed
        assert slot.generation == 1
        assert slot.observe().detector_id == "baseline"

        release.set()
        assert (await host.wait(operation.operation_id, 0.2)).lifecycle == Lifecycle.SUCCEEDED
        slot.close()
        await host.close()

    asyncio.run(scenario())


def test_marker_detection_rejects_invalid_geometry() -> None:
    with pytest.raises(ValueError, match="outside"):
        MarkerDetection(
            x_px=12.0,
            y_px=8.0,
            area_px=25,
            left_px=4,
            top_px=6,
            right_px=10,
            bottom_px=10,
        )

