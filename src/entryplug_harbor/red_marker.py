"""The approved RGB marker program, independent of ROS and transport.

This exact program is shared by Harbor's direct path and any named remote
worker. Image acquisition, freshness and worker provenance stay outside it.
"""

from __future__ import annotations

import numpy as np

from entryplug_harbor.perception import (
    DETECTOR_INTERFACE_VERSION,
    MarkerDetection,
    MarkerFrame,
)

RED_MARKER_DETECTOR_ID = "fixed-red-centroid-v1"


class FixedRedCentroidDetector:
    interface_version = DETECTOR_INTERFACE_VERSION
    detector_id = RED_MARKER_DETECTOR_ID

    def prepare(self) -> None:
        return None

    def detect(self, frame: MarkerFrame) -> MarkerDetection:
        if frame.encoding != "rgb8" or frame.step != frame.width * 3:
            raise RuntimeError(
                f"red marker detector requires packed rgb8, got {frame.encoding!r} "
                f"with step {frame.step}"
            )
        rgb = np.frombuffer(frame.data, dtype=np.uint8).reshape((frame.height, frame.width, 3))
        red = rgb[:, :, 0].astype(np.int16)
        green = rgb[:, :, 1].astype(np.int16)
        blue = rgb[:, :, 2].astype(np.int16)
        mask = (red >= 100) & (red >= green + 45) & (red >= blue + 45)
        rows, columns = np.nonzero(mask)
        if columns.size < 20:
            raise RuntimeError(f"red marker detector found only {columns.size} pixels")
        return MarkerDetection(
            x_px=round(float(np.mean(columns)), 6),
            y_px=round(float(np.mean(rows)), 6),
            area_px=int(columns.size),
            left_px=int(np.min(columns)),
            top_px=int(np.min(rows)),
            right_px=int(np.max(columns)),
            bottom_px=int(np.max(rows)),
        )

    def close(self) -> None:
        return None
