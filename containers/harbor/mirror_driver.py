#!/usr/bin/python3
"""Drive the unrelated rendered mechanism on a fixture-owned schedule."""

from __future__ import annotations

import itertools

import rclpy
from rclpy.node import Node
from std_msgs.msg import Float64MultiArray


SCHEDULE = (
    (0.0, -0.075),
    (0.025, -0.115),
    (-0.02, -0.045),
    (0.035, -0.095),
    (-0.03, -0.06),
)


class MirrorDriver(Node):
    def __init__(self) -> None:
        super().__init__("entryplug_mirror_fixture_driver")
        self._publisher = self.create_publisher(
            Float64MultiArray, "/mirror_controller/commands", 10
        )
        self._schedule = itertools.cycle(SCHEDULE)
        self._timer = self.create_timer(0.9, self._publish_next)
        self._publish_next()

    def _publish_next(self) -> None:
        message = Float64MultiArray()
        message.data = list(next(self._schedule))
        self._publisher.publish(message)


def main() -> int:
    rclpy.init()
    node = MirrorDriver()
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
