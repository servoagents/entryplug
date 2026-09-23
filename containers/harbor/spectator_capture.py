#!/usr/bin/python3
"""Capture evaluator-only imagery after the association process has exited."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import cv2
import numpy as np
import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import Image


SPECTATOR_TOPIC = "/camera/spectator/image_raw"


class SpectatorCapture(Node):
    """A fixture evaluator that is deliberately separate from source association."""

    def __init__(self) -> None:
        super().__init__("entryplug_spectator_evaluator")
        self.message: Image | None = None
        self.create_subscription(
            Image,
            SPECTATOR_TOPIC,
            self._receive,
            qos_profile_sensor_data,
        )

    def _receive(self, message: Image) -> None:
        self.message = message


def _write_capture(path: Path, message: Image) -> None:
    if message.encoding != "rgb8" or message.step != message.width * 3:
        raise RuntimeError(
            f"spectator capture requires packed rgb8, got {message.encoding!r} "
            f"with step {message.step}"
        )
    rgb = np.frombuffer(bytes(message.data), dtype=np.uint8).reshape(
        (message.height, message.width, 3)
    )
    encoded, contents = cv2.imencode(".png", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
    if not encoded:
        raise RuntimeError("OpenCV could not encode the spectator capture")
    with path.open("xb") as stream:
        stream.write(contents.tobytes())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args()

    rclpy.init()
    node = SpectatorCapture()
    try:
        deadline = time.monotonic() + args.timeout
        while rclpy.ok() and node.message is None and time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=0.1)
        if node.message is None:
            raise TimeoutError(f"no image received from {SPECTATOR_TOPIC}")
        _write_capture(args.output, node.message)
        print(
            json.dumps(
                {
                    "status": "captured",
                    "topic": SPECTATOR_TOPIC,
                    "role": "evaluator_only",
                    "output": str(args.output),
                },
                sort_keys=True,
            )
        )
        return 0
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
