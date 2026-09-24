#!/usr/bin/python3
"""Drive the unrelated rendered mechanism on a hidden, nonperiodic schedule."""

from __future__ import annotations

import argparse
import json
import random
import time
from pathlib import Path

import rclpy
from rclpy.node import Node
from std_msgs.msg import Float64MultiArray

from entryplug.seeding import derive_experiment_seed

DEFAULT_FIXTURE_SEED = 3119


class MirrorDriver(Node):
    def __init__(
        self,
        evaluation_output: Path,
        fixture_seed: int,
        experiment_seed: int | None,
    ) -> None:
        super().__init__("entryplug_mirror_fixture_driver")
        self._evidence = evaluation_output.open("x", encoding="utf-8")
        self._event_index = 0
        self._write_evidence(
            {
                "event": "fixture_schedule_started",
                "generator": "python_random_mt19937",
                "experiment_seed": experiment_seed,
                "fixture_seed": fixture_seed,
                "probe_topics_subscribed": [],
                "command_topic": "/mirror_controller/commands",
            }
        )
        self._publisher = self.create_publisher(
            Float64MultiArray, "/mirror_controller/commands", 10
        )
        self._random = random.Random(fixture_seed)
        self._timer = None
        self._publish_next()

    def _publish_next(self) -> None:
        if self._timer is not None:
            self.destroy_timer(self._timer)
        positions = (
            self._random.uniform(-0.035, 0.035),
            self._random.uniform(-0.12, -0.04),
        )
        hold_seconds = self._random.uniform(0.55, 1.8)
        message = Float64MultiArray()
        message.data = list(positions)
        self._publisher.publish(message)
        self._event_index += 1
        self._write_evidence(
            {
                "event": "fixture_command_published",
                "index": self._event_index,
                "published_monotonic": time.monotonic(),
                "positions_radians": list(positions),
                "hold_seconds": hold_seconds,
            }
        )
        self._timer = self.create_timer(hold_seconds, self._publish_next)

    def _write_evidence(self, record: dict[str, object]) -> None:
        self._evidence.write(json.dumps(record, sort_keys=True))
        self._evidence.write("\n")
        self._evidence.flush()

    def close_evidence(self) -> None:
        self._evidence.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evaluation-output", type=Path, required=True)
    parser.add_argument("--experiment-seed", type=int)
    args = parser.parse_args()
    fixture_seed = (
        DEFAULT_FIXTURE_SEED
        if args.experiment_seed is None
        else derive_experiment_seed(args.experiment_seed, "independent_fixture")
    )
    rclpy.init()
    node = MirrorDriver(args.evaluation_output, fixture_seed, args.experiment_seed)
    try:
        rclpy.spin(node)
    finally:
        node.close_evidence()
        node.destroy_node()
        rclpy.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
