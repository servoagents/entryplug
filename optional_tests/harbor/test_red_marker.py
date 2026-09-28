"""The same approved pixel program works on plain frames without ROS objects."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pytest

from entryplug_harbor.red_marker import FixedRedCentroidDetector


@dataclass(frozen=True)
class PlainFrame:
    width: int
    height: int
    data: bytes
    encoding: str = "rgb8"

    @property
    def step(self) -> int:
        return self.width * 3


def test_detector_preserves_exact_centroid_area_and_bounds() -> None:
    rgb = np.zeros((240, 320, 3), dtype=np.uint8)
    rgb[100:120, 145:175] = (200, 0, 0)
    detector = FixedRedCentroidDetector()

    result = detector.detect(PlainFrame(320, 240, rgb.tobytes()))

    assert result.x_px == 159.5
    assert result.y_px == 109.5
    assert result.area_px == 600
    assert (result.left_px, result.top_px, result.right_px, result.bottom_px) == (
        145,
        100,
        174,
        119,
    )


def test_detector_rejects_dark_or_unpacked_input() -> None:
    detector = FixedRedCentroidDetector()
    dark = PlainFrame(4, 4, bytes(4 * 4 * 3))
    with pytest.raises(RuntimeError, match="found only 0 pixels"):
        detector.detect(dark)
    with pytest.raises(RuntimeError, match="requires packed rgb8"):
        detector.detect(PlainFrame(4, 4, dark.data, "bgr8"))
