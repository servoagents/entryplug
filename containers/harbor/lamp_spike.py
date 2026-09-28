#!/usr/bin/python3
"""Evaluator-only proof that a queued MuJoCo lamp command changes rendered RGB."""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Callable
from collections import deque
from pathlib import Path

import cv2
import numpy as np
import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import Image
from std_msgs.msg import Float32, Float64MultiArray

IMAGE_TOPIC = "/camera/spectator/image_raw"
COMMAND_TOPIC = "/entryplug_fixture_lamp/command"
STATE_TOPIC = "/entryplug_fixture_lamp/state"
MIN_RGB_DELTA = 5.0  # Development-only feasibility threshold, not a frozen task profile.


def _stamp_seconds(message: Image) -> float:
    return message.header.stamp.sec + message.header.stamp.nanosec / 1_000_000_000


def _rgb(message: Image) -> np.ndarray:
    if message.encoding != "rgb8" or message.step != message.width * 3:
        raise ValueError("fixture camera must publish packed rgb8")
    if len(message.data) != message.width * message.height * 3:
        raise ValueError("fixture camera image length is invalid")
    return np.frombuffer(bytes(message.data), dtype=np.uint8).reshape(
        message.height, message.width, 3
    )


class LampSpike(Node):
    def __init__(self) -> None:
        super().__init__("entryplug_lamp_spike_evaluator")
        self.images: deque[Image] = deque(maxlen=20)
        self.states: deque[Float64MultiArray] = deque(maxlen=20)
        self.command = self.create_publisher(Float32, COMMAND_TOPIC, 1)
        self.create_subscription(Image, IMAGE_TOPIC, self._receive_image, qos_profile_sensor_data)
        self.create_subscription(Float64MultiArray, STATE_TOPIC, self._receive_state, 10)

    def _receive_image(self, message: Image) -> None:
        self.images.append(message)

    def _receive_state(self, message: Float64MultiArray) -> None:
        self.states.append(message)

    def _spin_until(self, predicate: Callable[[], bool], timeout_s: float, label: str) -> None:
        deadline = time.monotonic() + timeout_s
        while rclpy.ok() and not predicate() and time.monotonic() < deadline:
            rclpy.spin_once(self, timeout_sec=0.1)
        if not predicate():
            raise TimeoutError(f"timed out waiting for {label}")

    def applied_state(self, level: float, timeout_s: float = 12.0) -> tuple[float, int]:
        def matching() -> bool:
            return any(
                len(state.data) == 3 and abs(state.data[0] - level) < 0.001
                for state in self.states
            )

        self._spin_until(matching, timeout_s, f"applied lamp level {level}")
        matching_states = [
            state for state in self.states
            if len(state.data) == 3 and abs(state.data[0] - level) < 0.001
        ]
        selected = matching_states[-1]
        return float(selected.data[1]), int(selected.data[2])

    def set_level(self, level: float) -> None:
        self._spin_until(
            lambda: self.command.get_subscription_count() > 0,
            10.0,
            "fixture lamp subscriber",
        )
        message = Float32()
        message.data = level
        self.command.publish(message)

    def captures_after(
        self, applied_sim_time: float, label: str, output_dir: Path
    ) -> list[dict[str, object]]:
        selected: list[dict[str, object]] = []
        seen_stamps: set[float] = set()
        deadline = time.monotonic() + 12.0
        while len(selected) < 2 and time.monotonic() < deadline:
            rclpy.spin_once(self, timeout_sec=0.1)
            while self.images:
                message = self.images.popleft()
                stamp = _stamp_seconds(message)
                if stamp <= applied_sim_time or stamp in seen_stamps:
                    continue
                rgb = _rgb(message)
                path = output_dir / f"{label}-{len(selected) + 1}.png"
                encoded, image = cv2.imencode(".png", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
                if not encoded:
                    raise RuntimeError("could not encode a camera frame")
                with path.open("xb") as stream:
                    stream.write(image.tobytes())
                selected.append(
                    {
                        "sim_stamp_s": stamp,
                        "mean_rgb": [float(value) for value in rgb.mean(axis=(0, 1))],
                        "mean_intensity": float(rgb.mean()),
                        "image": path.name,
                    }
                )
                seen_stamps.add(stamp)
                if len(selected) == 2:
                    break
        if len(selected) != 2:
            raise TimeoutError(f"two post-apply {label} frames were not available")
        return selected


def run(output: Path) -> dict[str, object]:
    rclpy.init()
    node = LampSpike()
    try:
        dark_time, dark_revision = node.applied_state(0.0)
        dark = node.captures_after(dark_time, "dark", output.parent)
        node.set_level(0.75)
        bright_time, bright_revision = node.applied_state(0.75)
        if bright_revision <= dark_revision or bright_time <= dark_time:
            raise RuntimeError("lamp apply boundary did not progress")
        bright = node.captures_after(bright_time, "bright", output.parent)
        dark_mean = sum(frame["mean_intensity"] for frame in dark) / 2
        bright_mean = sum(frame["mean_intensity"] for frame in bright) / 2
        delta = bright_mean - dark_mean
        passed = delta >= MIN_RGB_DELTA
        return {
            "status": "passed" if passed else "failed",
            "reason": "RENDERED_RGB_CHANGED" if passed else "INSUFFICIENT_RGB_CHANGE",
            "fixture": "hall-of-mirrors stationary spectator camera, no arm commands",
            "commanded_level": 0.75,
            "dark_applied_sim_time": dark_time,
            "bright_applied_sim_time": bright_time,
            "dark_revision": dark_revision,
            "bright_revision": bright_revision,
            "dark": dark,
            "bright": bright,
            "mean_intensity_delta": delta,
            "minimum_rgb_delta": MIN_RGB_DELTA,
        }
    finally:
        node.destroy_node()
        rclpy.shutdown()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = run(args.output)
    except Exception as error:
        report = {"status": "failed", "reason": type(error).__name__, "detail": str(error)}
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, sort_keys=True)
        stream.write("\n")
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
