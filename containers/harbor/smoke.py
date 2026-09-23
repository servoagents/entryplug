#!/usr/bin/python3
"""Observe one real camera frame and one bounded trajectory through public ROS APIs."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

import rclpy
from action_msgs.msg import GoalStatus
from builtin_interfaces.msg import Duration
from control_msgs.action import FollowJointTrajectory
from rclpy.action import ActionClient
from rclpy.duration import Duration as RclpyDuration
from rclpy.node import Node
from rclpy.parameter import Parameter
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import Image, JointState
from trajectory_msgs.msg import JointTrajectoryPoint


JOINTS = ("joint1", "joint2")
DELTA = (0.02, -0.02)
POSITION_TOLERANCE = 0.01


@dataclass(frozen=True, slots=True)
class Frame:
    sequence: int
    received_monotonic: float
    width: int
    height: int
    encoding: str
    step: int
    stamp_sec: int
    stamp_nanosec: int
    sha256: str

    def public_dict(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "width": self.width,
            "height": self.height,
            "encoding": self.encoding,
            "step": self.step,
            "stamp": {"sec": self.stamp_sec, "nanosec": self.stamp_nanosec},
            "sha256": self.sha256,
        }


class Observer(Node):
    def __init__(self) -> None:
        super().__init__(
            "entryplug_harbor_observer",
            parameter_overrides=[Parameter("use_sim_time", value=True)],
        )
        self.frame: Frame | None = None
        self.joints: JointState | None = None
        self._frame_sequence = 0
        self.create_subscription(
            Image, "/camera/color/image_raw", self._image, qos_profile_sensor_data
        )
        self.create_subscription(
            JointState, "/joint_states", self._joint_state, qos_profile_sensor_data
        )
        self.action = ActionClient(
            self,
            FollowJointTrajectory,
            "/trajectory_controller/follow_joint_trajectory",
        )

    def _image(self, message: Image) -> None:
        self._frame_sequence += 1
        self.frame = Frame(
            sequence=self._frame_sequence,
            received_monotonic=time.monotonic(),
            width=message.width,
            height=message.height,
            encoding=message.encoding,
            step=message.step,
            stamp_sec=message.header.stamp.sec,
            stamp_nanosec=message.header.stamp.nanosec,
            sha256=hashlib.sha256(bytes(message.data)).hexdigest(),
        )

    def _joint_state(self, message: JointState) -> None:
        self.joints = message


def _spin_until(node: Node, predicate: Callable[[], bool], timeout: float, label: str) -> None:
    deadline = time.monotonic() + timeout
    while rclpy.ok() and time.monotonic() < deadline:
        rclpy.spin_once(node, timeout_sec=min(0.1, max(0.0, deadline - time.monotonic())))
        if predicate():
            return
    raise TimeoutError(f"timed out waiting for {label}")


def _future(node: Node, future: Any, timeout: float, label: str) -> Any:
    _spin_until(node, future.done, timeout, label)
    error = future.exception()
    if error is not None:
        raise RuntimeError(f"{label} failed: {error}")
    return future.result()


def _positions(message: JointState) -> dict[str, float]:
    values = dict(zip(message.name, message.position, strict=False))
    missing = [name for name in JOINTS if name not in values]
    if missing:
        raise RuntimeError(f"joint state omitted {', '.join(missing)}")
    selected = {name: float(values[name]) for name in JOINTS}
    if not all(math.isfinite(value) for value in selected.values()):
        raise RuntimeError("joint state contained a non-finite position")
    return selected


def _write_create_only(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2, sort_keys=True)
        stream.write("\n")


def qualify() -> dict[str, object]:
    started = time.monotonic()
    node = Observer()
    try:
        _spin_until(
            node,
            lambda: node.frame is not None and node.joints is not None,
            30.0,
            "camera and joint topics",
        )
        baseline = node.frame
        joint_message = node.joints
        assert baseline is not None and joint_message is not None
        if baseline.width <= 0 or baseline.height <= 0 or not baseline.encoding:
            raise RuntimeError("camera published an invalid image description")
        if baseline.stamp_sec == 0 and baseline.stamp_nanosec == 0:
            raise RuntimeError("camera frame used a zero timestamp")
        before = _positions(joint_message)

        if not node.action.wait_for_server(timeout_sec=15.0):
            raise TimeoutError("trajectory action server was unavailable")

        goal = FollowJointTrajectory.Goal()
        goal.trajectory.joint_names = list(JOINTS)
        now = (node.get_clock().now() + RclpyDuration(seconds=0.2)).to_msg()
        if now.sec == 0 and now.nanosec == 0:
            raise RuntimeError("ROS clock was not active")
        goal.trajectory.header.stamp = now
        point = JointTrajectoryPoint()
        point.positions = [before[name] + change for name, change in zip(JOINTS, DELTA)]
        point.time_from_start = Duration(sec=1, nanosec=0)
        goal.trajectory.points = [point]

        submitted = time.monotonic()
        goal_handle = _future(
            node,
            node.action.send_goal_async(goal),
            10.0,
            "trajectory goal acceptance",
        )
        if not goal_handle.accepted:
            raise RuntimeError("trajectory action rejected the bounded goal")
        result = _future(node, goal_handle.get_result_async(), 15.0, "trajectory result")
        completed = time.monotonic()
        if result.status != GoalStatus.STATUS_SUCCEEDED:
            raise RuntimeError(f"trajectory action finished with status {result.status}")

        _spin_until(
            node,
            lambda: node.frame is not None and node.frame.sequence > baseline.sequence,
            10.0,
            "a fresh camera frame after the command",
        )
        after_frame = node.frame
        assert after_frame is not None
        _spin_until(
            node,
            lambda: node.joints is not None
            and all(
                abs(_positions(node.joints)[name] - target) <= POSITION_TOLERANCE
                for name, target in zip(JOINTS, point.positions)
            ),
            5.0,
            "bounded joint feedback",
        )
        assert node.joints is not None
        after = _positions(node.joints)
        observed_delta = {name: after[name] - before[name] for name in JOINTS}
        for name, requested in zip(JOINTS, DELTA):
            observed = observed_delta[name]
            if observed * requested <= 0 or abs(observed) < abs(requested) * 0.5:
                raise RuntimeError(f"{name} feedback did not show the commanded movement")
        changed = after_frame.sha256 != baseline.sha256
        if not changed:
            raise RuntimeError("fresh camera frame did not change after the trajectory")

        return {
            "schema_version": 1,
            "status": "passed",
            "recorded_at": datetime.now(UTC).isoformat(),
            "interfaces": {
                "camera_topic": "/camera/color/image_raw",
                "joint_topic": "/joint_states",
                "trajectory_action": "/trajectory_controller/follow_joint_trajectory",
            },
            "camera": {
                "baseline": baseline.public_dict(),
                "after": after_frame.public_dict(),
                "content_changed": changed,
            },
            "trajectory": {
                "joint_names": list(JOINTS),
                "before": before,
                "commanded": dict(zip(JOINTS, point.positions)),
                "after": after,
                "observed_delta": observed_delta,
                "goal_status": result.status,
                "maximum_delta_radians": max(abs(value) for value in DELTA),
                "position_tolerance_radians": POSITION_TOLERANCE,
            },
            "timing_ms": {
                "action_result": round((completed - submitted) * 1000, 3),
                "first_post_command_frame": round(
                    (after_frame.received_monotonic - submitted) * 1000, 3
                ),
                "total": round((time.monotonic() - started) * 1000, 3),
            },
            "claim_boundary": (
                "Public ROS observations only; no simulator-internal ground truth is claimed."
            ),
        }
    finally:
        node.destroy_node()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rclpy.init()
    try:
        try:
            payload = qualify()
        except Exception as error:  # preserve the failure as evidence at the process boundary
            payload = {
                "schema_version": 1,
                "status": "failed",
                "recorded_at": datetime.now(UTC).isoformat(),
                "error_type": type(error).__name__,
                "error": str(error),
                "claim_boundary": "No camera or actuation capability is claimed.",
            }
            _write_create_only(args.output, payload)
            print(f"harbor smoke failed: {error}")
            return 1
        _write_create_only(args.output, payload)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    finally:
        rclpy.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
