#!/usr/bin/python3
"""Live, ROS-local inspection of the stationary panel (not the HA/Zenoh lane)."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import Image
from std_msgs.msg import Float32, Float64MultiArray

from entryplug.core.evidence import json_object
from entryplug.core.operation import Lifecycle, OperationHost
from entryplug.embodiment.inspection import (
    CameraFrame,
    LightReport,
    MarkerMeasurement,
    inspection_spec,
)
from entryplug.harness.session import Session
from entryplug_harbor.red_marker import FixedRedCentroidDetector

IMAGE_TOPIC = "/camera/panel/image_raw"
COMMAND_TOPIC = "/entryplug_fixture_lamp/command"
STATE_TOPIC = "/entryplug_fixture_lamp/state"


def _sim_stamp(message: Image) -> float:
    return message.header.stamp.sec + message.header.stamp.nanosec / 1_000_000_000


@dataclass(frozen=True, slots=True)
class _DetectorFrame:
    width: int
    height: int
    data: bytes
    encoding: str = "rgb8"

    @property
    def step(self) -> int:
        return self.width * 3


class LocalPanelWorker:
    """The approved detector in-process for this intermediate live qualification."""

    worker_id = "local-panel-worker-v1"
    program_id = FixedRedCentroidDetector.detector_id

    def __init__(self) -> None:
        self._detector = FixedRedCentroidDetector()
        self._detector.prepare()

    async def detect(self, frame: CameraFrame) -> MarkerMeasurement:
        image = _DetectorFrame(frame.width, frame.height, frame.rgb8)
        try:
            marker = self._detector.detect(image)
        except RuntimeError as error:
            if not str(error).startswith("red marker detector found only"):
                raise
            return MarkerMeasurement(
                frame.source_id,
                frame.sample_id,
                self.worker_id,
                self.program_id,
                None,
                None,
                0.0,
            )
        # Pixel area is measured, not a fixture-provided visibility flag.
        quality = min(marker.area_px / 1_000, 1.0)
        return MarkerMeasurement(
            frame.source_id,
            frame.sample_id,
            self.worker_id,
            self.program_id,
            marker.x_px,
            marker.y_px,
            quality,
        )

    def close(self) -> None:
        self._detector.close()


class PanelFixture(Node):
    """ROS-facing camera and lamp ports with a shared simulation-time barrier."""

    entity_id = "fixture.panel_lamp"

    def __init__(self, output_dir: Path, *, direct_lamp: bool = True) -> None:
        super().__init__("entryplug_panel_inspection")
        self.output_dir = output_dir
        self.sequence = 0
        self.latest_image: tuple[Image, int, float] | None = None
        self.latest_state: tuple[float, float, int] | None = None
        self.minimum_sim_stamp = -1.0
        self.frame_artifacts: list[str] = []
        self.frames: list[dict[str, object]] = []
        self.applied_states: list[dict[str, float | int]] = []
        self.command = self.create_publisher(Float32, COMMAND_TOPIC, 1) if direct_lamp else None
        self.create_subscription(Image, IMAGE_TOPIC, self._on_image, qos_profile_sensor_data)
        self.create_subscription(Float64MultiArray, STATE_TOPIC, self._on_state, 10)

    def _on_image(self, message: Image) -> None:
        self.sequence += 1
        self.latest_image = (message, self.sequence, time.monotonic())

    def _on_state(self, message: Float64MultiArray) -> None:
        if len(message.data) != 3:
            return
        self.latest_state = (float(message.data[0]), float(message.data[1]), int(message.data[2]))

    def _spin_until(self, condition: Callable[[], bool], label: str, timeout_s: float) -> None:
        deadline = time.monotonic() + timeout_s
        while rclpy.ok() and time.monotonic() < deadline:
            if condition():
                return
            rclpy.spin_once(self, timeout_sec=0.1)
        raise TimeoutError(f"timed out waiting for {label}")

    def _capture(self) -> CameraFrame:
        baseline = self.sequence

        def ready() -> bool:
            if self.latest_image is None or self.latest_image[1] <= baseline:
                return False
            return _sim_stamp(self.latest_image[0]) > self.minimum_sim_stamp

        self._spin_until(ready, "post-boundary panel frame", 5.0)
        assert self.latest_image is not None
        message, sequence, received = self.latest_image
        if message.encoding != "rgb8" or message.step != message.width * 3:
            raise ValueError("panel camera must publish packed rgb8")
        rgb8 = bytes(message.data)
        if len(rgb8) != message.width * message.height * 3:
            raise ValueError("panel camera image length is invalid")
        frame = CameraFrame(
            "panel-camera",
            "panel-camera-rgb8-v1",
            f"panel-{message.header.stamp.sec}-{message.header.stamp.nanosec}",
            sequence,
            received,
            message.width,
            message.height,
            rgb8,
        )
        rgb = np.frombuffer(rgb8, dtype=np.uint8).reshape(message.height, message.width, 3)
        encoded, image = cv2.imencode(".png", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
        if not encoded:
            raise RuntimeError("could not encode panel frame")
        name = f"panel-frame-{sequence:04d}.png"
        with (self.output_dir / name).open("xb") as stream:
            stream.write(image.tobytes())
        self.frame_artifacts.append(name)
        self.frames.append(
            {
                "sample_id": frame.sample_id,
                "sequence": sequence,
                "source_id": frame.source_id,
                "lineage_id": frame.lineage_id,
                "captured_sim_time_s": _sim_stamp(message),
                "received_monotonic": received,
                "artifact": name,
                "artifact_sha256": hashlib.sha256(image.tobytes()).hexdigest(),
                "input_sha256": hashlib.sha256(rgb8).hexdigest(),
            }
        )
        return frame

    async def capture(self) -> CameraFrame:
        return await asyncio.to_thread(self._capture)

    def _set_brightness(self, level: float) -> LightReport:
        if self.command is None:
            raise RuntimeError("direct ROS light control is not bound to this camera")
        self._spin_until(lambda: self.latest_state is not None, "initial lamp state", 8.0)
        self._spin_until(lambda: self.command.get_subscription_count() > 0, "lamp subscriber", 8.0)
        assert self.latest_state is not None
        prior_revision = self.latest_state[2]
        command = Float32()
        command.data = level
        self.command.publish(command)

        def applied() -> bool:
            return (
                self.latest_state is not None
                and self.latest_state[2] > prior_revision
                and abs(self.latest_state[0] - level) <= 0.001
            )

        self._spin_until(applied, "applied lamp revision", 8.0)
        assert self.latest_state is not None
        reported_level, applied_sim_time, revision = self.latest_state
        self.minimum_sim_stamp = applied_sim_time
        self.applied_states.append(
            {"level": reported_level, "sim_time_s": applied_sim_time, "revision": revision}
        )
        return LightReport(self.entity_id, reported_level, True)

    async def set_brightness(self, level: float) -> LightReport:
        return await asyncio.to_thread(self._set_brightness, level)


async def run(output: Path) -> dict[str, object]:
    rclpy.init()
    fixture = PanelFixture(output.parent)
    worker = LocalPanelWorker()
    host = OperationHost(
        (inspection_spec(fixture, worker, light=fixture),), runtime_id="panel-inspection"
    )
    session = Session(host, owns_runtime=True)
    try:
        operation = await session.act(
            "inspect_target", {"target_id": "bench-marker"}, request_id="panel-inspection"
        )
        result = await session.wait(operation, 35.0)
        inspection = json_object(
            await session.inspect(operation.operation_id, detail="result"), "panel inspection"
        )
        return {
            "status": "passed" if result.lifecycle == Lifecycle.SUCCEEDED else "failed",
            "fixture": "stationary marked panel, no joints or arm commands",
            "transport": "ROS-local camera and lamp; in-process approved detector",
            "operation": inspection,
            "applied_states": fixture.applied_states,
            "frame_artifacts": fixture.frame_artifacts,
        }
    finally:
        await session.close()
        worker.close()
        fixture.destroy_node()
        rclpy.shutdown()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = asyncio.run(run(args.output))
    except Exception as error:
        report = {"status": "failed", "reason": type(error).__name__, "detail": str(error)}
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
