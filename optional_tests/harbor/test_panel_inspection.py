"""The local panel worker reports measured pixels, never fixture visibility truth."""

from __future__ import annotations

import asyncio
import time

import numpy as np
from panel_inspection import LocalPanelWorker

from entryplug.embodiment.inspection import CameraFrame


def _frame(rgb: np.ndarray, sequence: int) -> CameraFrame:
    height, width, _ = rgb.shape
    return CameraFrame(
        "panel-camera",
        "panel-camera-rgb8-v1",
        f"sample-{sequence}",
        sequence,
        time.monotonic(),
        width,
        height,
        rgb.tobytes(),
    )


def test_local_panel_worker_uses_measured_red_pixels() -> None:
    worker = LocalPanelWorker()
    try:
        dark = np.zeros((240, 320, 3), dtype=np.uint8)
        no_marker = asyncio.run(worker.detect(_frame(dark, 1)))
        assert no_marker.centroid_x is None
        assert no_marker.centroid_y is None
        assert no_marker.quality == 0.0

        bright = dark.copy()
        bright[100:120, 145:175] = (200, 0, 0)
        marker = asyncio.run(worker.detect(_frame(bright, 2)))
        assert marker.worker_id == worker.worker_id
        assert marker.program_id == worker.program_id
        assert marker.centroid_x == 159.5
        assert marker.centroid_y == 109.5
        assert marker.quality == 0.6
    finally:
        worker.close()
