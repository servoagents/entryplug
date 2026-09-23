#!/usr/bin/python3
"""Qualify rendered motion and ROS action lifecycle behavior through public APIs."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np
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
COMPLETION_DELTA = (0.02, -0.02)
CANCEL_DELTA = (0.12, -0.08)
LOST_RESULT_DELTA = (-0.02, 0.015)
CLIENT_DEATH_DELTA = (0.02, 0.01)
POSITION_TOLERANCE = 0.01
HOLD_TOLERANCE = 0.004


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
    data: bytes

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
    def __init__(self, name: str = "entryplug_harbor_observer") -> None:
        super().__init__(
            name,
            parameter_overrides=[Parameter("use_sim_time", value=True)],
        )
        self.frame: Frame | None = None
        self.frames: list[Frame] = []
        self.joints: JointState | None = None
        self.joint_sequence = 0
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
        data = bytes(message.data)
        frame = Frame(
            sequence=(self.frame.sequence + 1) if self.frame else 1,
            received_monotonic=time.monotonic(),
            width=message.width,
            height=message.height,
            encoding=message.encoding,
            step=message.step,
            stamp_sec=message.header.stamp.sec,
            stamp_nanosec=message.header.stamp.nanosec,
            sha256=hashlib.sha256(data).hexdigest(),
            data=data,
        )
        self.frame = frame
        self.frames.append(frame)
        if len(self.frames) > 256:
            del self.frames[:128]

    def _joint_state(self, message: JointState) -> None:
        self.joint_sequence += 1
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


def _current_positions(node: Observer) -> dict[str, float]:
    if node.joints is None:
        raise RuntimeError("joint state is unavailable")
    return _positions(node.joints)


def _velocities(message: JointState) -> dict[str, float]:
    values = dict(zip(message.name, message.velocity, strict=False))
    missing = [name for name in JOINTS if name not in values]
    if missing:
        raise RuntimeError(f"joint state omitted velocity for {', '.join(missing)}")
    selected = {name: float(values[name]) for name in JOINTS}
    if not all(math.isfinite(value) for value in selected.values()):
        raise RuntimeError("joint state contained a non-finite velocity")
    return selected


def _wait_stationary(
    node: Observer,
    *,
    threshold_radians_s: float = 0.01,
    consecutive_samples: int = 5,
    timeout: float = 3.0,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    last_sequence = node.joint_sequence
    consecutive = 0
    latest: dict[str, float] = {}
    while rclpy.ok() and time.monotonic() < deadline:
        rclpy.spin_once(node, timeout_sec=min(0.05, deadline - time.monotonic()))
        if node.joint_sequence == last_sequence or node.joints is None:
            continue
        last_sequence = node.joint_sequence
        latest = _velocities(node.joints)
        if all(abs(value) <= threshold_radians_s for value in latest.values()):
            consecutive += 1
            if consecutive >= consecutive_samples:
                return {
                    "confirmed": True,
                    "threshold_radians_s": threshold_radians_s,
                    "consecutive_samples": consecutive,
                    "latest_velocity_radians_s": latest,
                }
        else:
            consecutive = 0
    raise TimeoutError(
        f"timed out waiting for stationary public feedback; latest velocities: {latest}"
    )


def _target(before: dict[str, float], delta: tuple[float, float]) -> dict[str, float]:
    return {name: before[name] + change for name, change in zip(JOINTS, delta)}


def _goal(node: Observer, target: dict[str, float], duration_s: float) -> Any:
    goal = FollowJointTrajectory.Goal()
    goal.trajectory.joint_names = list(JOINTS)
    stamp = (node.get_clock().now() + RclpyDuration(seconds=0.2)).to_msg()
    if stamp.sec == 0 and stamp.nanosec == 0:
        raise RuntimeError("ROS clock was not active")
    goal.trajectory.header.stamp = stamp
    point = JointTrajectoryPoint()
    point.positions = [target[name] for name in JOINTS]
    whole_seconds = int(duration_s)
    point.time_from_start = Duration(
        sec=whole_seconds,
        nanosec=int((duration_s - whole_seconds) * 1_000_000_000),
    )
    goal.trajectory.points = [point]
    return goal


def _send_goal(node: Observer, target: dict[str, float], duration_s: float) -> tuple[Any, float]:
    submitted = time.monotonic()
    handle = _future(
        node,
        node.action.send_goal_async(_goal(node, target, duration_s)),
        10.0,
        "trajectory goal acceptance",
    )
    if not handle.accepted:
        raise RuntimeError("trajectory action rejected the bounded goal")
    return handle, submitted


def _goal_id(handle: Any) -> str:
    return bytes(handle.goal_id.uuid).hex()


def _wait_position(
    node: Observer,
    target: dict[str, float],
    *,
    tolerance: float = POSITION_TOLERANCE,
    timeout: float = 5.0,
    label: str = "bounded joint feedback",
) -> dict[str, float]:
    _spin_until(
        node,
        lambda: node.joints is not None
        and all(
            abs(_positions(node.joints)[name] - target[name]) <= tolerance
            for name in JOINTS
        ),
        timeout,
        label,
    )
    return _current_positions(node)


def _observe_hold(node: Observer, duration_s: float = 0.6) -> dict[str, object]:
    start = _current_positions(node)
    initial_sequence = node.joint_sequence
    maximum_drift = {name: 0.0 for name in JOINTS}
    deadline = time.monotonic() + duration_s
    while rclpy.ok() and time.monotonic() < deadline:
        rclpy.spin_once(node, timeout_sec=min(0.05, deadline - time.monotonic()))
        current = _current_positions(node)
        for name in JOINTS:
            maximum_drift[name] = max(maximum_drift[name], abs(current[name] - start[name]))
    if node.joint_sequence <= initial_sequence:
        raise RuntimeError("no fresh joint feedback arrived during hold observation")
    if any(drift > HOLD_TOLERANCE for drift in maximum_drift.values()):
        raise RuntimeError(f"joint drift exceeded hold tolerance: {maximum_drift}")
    return {
        "confirmed": True,
        "duration_ms": round(duration_s * 1000, 3),
        "tolerance_radians": HOLD_TOLERANCE,
        "maximum_drift_radians": maximum_drift,
        "fresh_samples": node.joint_sequence - initial_sequence,
    }


def _write_create_only(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2, sort_keys=True)
        stream.write("\n")


def _write_png_create_only(path: Path, frame: Frame) -> None:
    if frame.encoding != "rgb8" or frame.step != frame.width * 3:
        raise RuntimeError(
            f"cannot preserve {frame.encoding!r} frame with step {frame.step} as PNG"
        )
    rgb = np.frombuffer(frame.data, dtype=np.uint8).reshape((frame.height, frame.width, 3))
    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    encoded, contents = cv2.imencode(".png", bgr)
    if not encoded:
        raise RuntimeError("OpenCV could not encode the observed camera frame")
    with path.open("xb") as stream:
        stream.write(contents.tobytes())


def _frame_difference(before: Frame, after: Frame) -> dict[str, object]:
    if (before.width, before.height, before.encoding, before.step) != (
        after.width,
        after.height,
        after.encoding,
        after.step,
    ):
        raise RuntimeError("camera frame description changed during qualification")
    if len(before.data) != len(after.data) or not before.data:
        raise RuntimeError("camera frame payload size changed or was empty")
    absolute_sum = 0
    changed_channels = 0
    for old, new in zip(before.data, after.data, strict=True):
        difference = abs(old - new)
        absolute_sum += difference
        changed_channels += difference != 0
    return {
        "content_changed": changed_channels > 0,
        "changed_channel_fraction": round(changed_channels / len(before.data), 8),
        "mean_absolute_channel_delta": round(absolute_sum / len(before.data), 6),
    }


def _first_effect_frame(node: Observer, baseline: Frame, submitted: float) -> Frame:
    candidates = [
        frame
        for frame in node.frames
        if frame.received_monotonic >= submitted and frame.sha256 != baseline.sha256
    ]
    if not candidates:
        raise RuntimeError("no changed camera frame was observed after the trajectory")
    return candidates[0]


def _completion_case(node: Observer, output_dir: Path) -> dict[str, object]:
    baseline = node.frame
    assert baseline is not None
    before = _current_positions(node)
    target = _target(before, COMPLETION_DELTA)
    handle, submitted = _send_goal(node, target, 1.0)
    result = _future(node, handle.get_result_async(), 15.0, "trajectory result")
    completed = time.monotonic()
    if result.status != GoalStatus.STATUS_SUCCEEDED:
        raise RuntimeError(f"trajectory action finished with status {result.status}")
    after = _wait_position(node, target)
    after_frame = node.frame
    assert after_frame is not None
    observed_delta = {name: after[name] - before[name] for name in JOINTS}
    for name, requested in zip(JOINTS, COMPLETION_DELTA):
        observed = observed_delta[name]
        if observed * requested <= 0 or abs(observed) < abs(requested) * 0.5:
            raise RuntimeError(f"{name} feedback did not show the commanded movement")
    difference = _frame_difference(baseline, after_frame)
    if not difference["content_changed"]:
        raise RuntimeError("fresh camera frame did not change after the trajectory")
    effect_frame = _first_effect_frame(node, baseline, submitted)
    _write_png_create_only(output_dir / "camera-before.png", baseline)
    _write_png_create_only(output_dir / "camera-after.png", after_frame)
    return {
        "status": "passed",
        "goal_id": _goal_id(handle),
        "native_status": result.status,
        "before": before,
        "commanded": target,
        "after": after,
        "observed_delta": observed_delta,
        "maximum_command_delta_radians": max(abs(value) for value in COMPLETION_DELTA),
        "position_tolerance_radians": POSITION_TOLERANCE,
        "camera": {
            "baseline": baseline.public_dict(),
            "after": after_frame.public_dict(),
            **difference,
        },
        "timing_ms": {
            "native_result": round((completed - submitted) * 1000, 3),
            "effect_feedback": round(
                (effect_frame.received_monotonic - submitted) * 1000, 3
            ),
        },
    }


def _cancellation_case(node: Observer) -> dict[str, object]:
    before = _current_positions(node)
    target = _target(before, CANCEL_DELTA)
    handle, submitted = _send_goal(node, target, 3.0)
    result_future = handle.get_result_async()
    _spin_until(
        node,
        lambda: abs(_current_positions(node)["joint1"] - before["joint1"]) >= 0.012,
        2.0,
        "observable motion before cancellation",
    )
    cancel_requested = time.monotonic()
    cancel_response = _future(
        node,
        handle.cancel_goal_async(),
        5.0,
        "cancel acknowledgment",
    )
    cancel_acknowledged = time.monotonic()
    acknowledged = bool(cancel_response.goals_canceling)
    if not acknowledged:
        raise RuntimeError("action server did not acknowledge cancellation")
    result = _future(node, result_future, 8.0, "canceled trajectory result")
    if result.status != GoalStatus.STATUS_CANCELED:
        raise RuntimeError(f"canceled trajectory finished with status {result.status}")
    stationary = _wait_stationary(node)
    stopped = _current_positions(node)
    if all(abs(stopped[name] - target[name]) <= POSITION_TOLERANCE for name in JOINTS):
        raise RuntimeError("cancellation arrived only after the full target was reached")
    hold = _observe_hold(node)
    return {
        "status": "passed",
        "goal_id": _goal_id(handle),
        "native_status": result.status,
        "before": before,
        "commanded": target,
        "stopped": stopped,
        "cancel_acknowledged": acknowledged,
        "stop_confirmed_from_feedback": True,
        "stationary": stationary,
        "hold": hold,
        "timing_ms": {
            "cancel_acknowledgment": round(
                (cancel_acknowledged - cancel_requested) * 1000, 3
            ),
            "submitted_to_cancel_request": round((cancel_requested - submitted) * 1000, 3),
        },
        "claim_boundary": "Cancel acknowledgment was not treated as proof of stopped motion.",
    }


def _lost_result_case(node: Observer) -> dict[str, object]:
    before = _current_positions(node)
    target = _target(before, LOST_RESULT_DELTA)
    handle, submitted = _send_goal(node, target, 1.0)
    _wait_position(
        node,
        target,
        timeout=5.0,
        label="public joint effect without requesting an action result",
    )
    stationary = _wait_stationary(node)
    after = _wait_position(node, target, label="settled lost-result joint effect")
    observed = time.monotonic()
    observed_delta = {name: after[name] - before[name] for name in JOINTS}
    for name, requested in zip(JOINTS, LOST_RESULT_DELTA):
        if observed_delta[name] * requested <= 0 or abs(observed_delta[name]) < abs(
            requested
        ) * 0.5:
            raise RuntimeError(f"{name} feedback did not show the lost-result movement")
    hold = _observe_hold(node)
    return {
        "status": "passed",
        "goal_id": _goal_id(handle),
        "result_requested": False,
        "outcome": "effect_observed_without_native_result",
        "before": before,
        "commanded": target,
        "after": after,
        "observed_delta": observed_delta,
        "stationary": stationary,
        "hold": hold,
        "timing_ms": {
            "submitted_to_observed_effect": round((observed - submitted) * 1000, 3)
        },
        "claim_boundary": "No native completion status is claimed and the command was not retried.",
    }


def _wait_process(
    node: Observer, process: subprocess.Popen[str], timeout: float
) -> tuple[float, str]:
    deadline = time.monotonic() + timeout
    while process.poll() is None and time.monotonic() < deadline:
        rclpy.spin_once(node, timeout_sec=0.05)
    if process.poll() is None:
        process.terminate()
        raise TimeoutError("client-death fixture did not exit")
    output, _ = process.communicate(timeout=1)
    if process.returncode != 0:
        raise RuntimeError(f"client-death fixture failed ({process.returncode}): {output.strip()}")
    return time.monotonic(), output


def _client_death_case(node: Observer, output_dir: Path) -> dict[str, object]:
    handoff = output_dir / "client-death-handoff.json"
    baseline = node.frame
    assert baseline is not None
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--orphan-client", str(handoff)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    exited, _ = _wait_process(node, process, 12.0)
    payload = json.loads(handoff.read_text(encoding="utf-8"))
    if payload.get("status") != "accepted":
        raise RuntimeError("client-death handoff did not record an accepted goal")
    target_value = payload.get("commanded")
    if not isinstance(target_value, dict):
        raise RuntimeError("client-death handoff omitted its commanded target")
    target = {name: float(target_value[name]) for name in JOINTS}
    before_value = payload.get("before")
    if not isinstance(before_value, dict):
        raise RuntimeError("client-death handoff omitted its initial positions")
    before = {name: float(before_value[name]) for name in JOINTS}
    _wait_position(
        node,
        target,
        timeout=8.0,
        label="accepted motion after its client process exited",
    )
    stationary = _wait_stationary(node)
    after = _wait_position(node, target, label="settled client-death joint effect")
    effect_observed = time.monotonic()
    observed_delta = {name: after[name] - before[name] for name in JOINTS}
    for name, requested in zip(JOINTS, CLIENT_DEATH_DELTA):
        if observed_delta[name] * requested <= 0 or abs(observed_delta[name]) < abs(
            requested
        ) * 0.5:
            raise RuntimeError(f"{name} feedback did not show the client-death movement")
    hold = _observe_hold(node)
    after_frame = node.frame
    assert after_frame is not None
    difference = _frame_difference(baseline, after_frame)
    if not difference["content_changed"]:
        raise RuntimeError("camera did not change after the client-death trajectory")
    return {
        "status": "passed",
        "goal_id": payload["goal_id"],
        "client_exit_status": process.returncode,
        "client_exited_before_effect": exited < effect_observed,
        "native_result_collected": False,
        "before": before,
        "commanded": target,
        "after": after,
        "observed_delta": observed_delta,
        "stationary": stationary,
        "hold": hold,
        "camera": {
            "baseline": baseline.public_dict(),
            "after": after_frame.public_dict(),
            **difference,
        },
        "timing_ms": {
            "client_exit_to_observed_effect": round((effect_observed - exited) * 1000, 3)
        },
        "claim_boundary": (
            "An accepted goal outlived its client; this is observed behavior, "
            "not a lease guarantee."
        ),
    }


def qualify(output_dir: Path) -> dict[str, object]:
    started = time.monotonic()
    node = Observer()
    try:
        _spin_until(
            node,
            lambda: node.frame is not None and node.joints is not None,
            30.0,
            "camera and joint topics",
        )
        assert node.frame is not None
        if node.frame.width <= 0 or node.frame.height <= 0 or not node.frame.encoding:
            raise RuntimeError("camera published an invalid image description")
        if node.frame.stamp_sec == 0 and node.frame.stamp_nanosec == 0:
            raise RuntimeError("camera frame used a zero timestamp")
        if not node.action.wait_for_server(timeout_sec=15.0):
            raise TimeoutError("trajectory action server was unavailable")

        completion = _completion_case(node, output_dir)
        _write_create_only(output_dir / "case-completion.json", completion)
        cancellation = _cancellation_case(node)
        _write_create_only(output_dir / "case-cancellation.json", cancellation)
        lost_result = _lost_result_case(node)
        _write_create_only(output_dir / "case-lost-result.json", lost_result)
        client_death = _client_death_case(node, output_dir)
        _write_create_only(output_dir / "case-client-death.json", client_death)
        return {
            "schema_version": 2,
            "status": "passed",
            "recorded_at": datetime.now(UTC).isoformat(),
            "interfaces": {
                "camera_topic": "/camera/color/image_raw",
                "joint_topic": "/joint_states",
                "trajectory_action": "/trajectory_controller/follow_joint_trajectory",
            },
            "cases": {
                "completion": completion,
                "cancellation": cancellation,
                "lost_result": lost_result,
                "client_death": client_death,
            },
            "timing_ms": {"total": round((time.monotonic() - started) * 1000, 3)},
            "claim_boundary": (
                "Public ROS observations only; no simulator-internal ground truth is claimed."
            ),
        }
    finally:
        node.destroy_node()


def orphan_client(handoff: Path) -> int:
    rclpy.init()
    node = Observer("entryplug_client_death_fixture")
    try:
        _spin_until(node, lambda: node.joints is not None, 10.0, "orphan client joint state")
        if not node.action.wait_for_server(timeout_sec=10.0):
            raise TimeoutError("orphan client action server was unavailable")
        before = _current_positions(node)
        target = _target(before, CLIENT_DEATH_DELTA)
        handle, _ = _send_goal(node, target, 1.2)
        _write_create_only(
            handoff,
            {
                "schema_version": 1,
                "status": "accepted",
                "recorded_at": datetime.now(UTC).isoformat(),
                "goal_id": _goal_id(handle),
                "before": before,
                "commanded": target,
                "result_requested": False,
            },
        )
        return 0
    finally:
        node.destroy_node()
        rclpy.shutdown()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--orphan-client", type=Path)
    args = parser.parse_args()
    if args.orphan_client is not None:
        return orphan_client(args.orphan_client)
    if args.output is None:
        parser.error("--output is required unless --orphan-client is used")

    rclpy.init()
    try:
        try:
            payload = qualify(args.output.parent)
        except Exception as error:  # preserve failure evidence at the process boundary
            payload = {
                "schema_version": 2,
                "status": "failed",
                "recorded_at": datetime.now(UTC).isoformat(),
                "error_type": type(error).__name__,
                "error": str(error),
                "claim_boundary": "No lifecycle capability is claimed.",
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
