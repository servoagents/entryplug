#!/usr/bin/python3
"""Fixture MQTT command -> ROS lamp apply -> MQTT state, in that order."""

from __future__ import annotations

import argparse
import json
import math
import signal
import threading
import time
from collections.abc import Callable
from pathlib import Path

import rclpy
from rclpy.node import Node
from rclpy.signals import SignalHandlerOptions
from std_msgs.msg import Float32, Float64MultiArray

from entryplug_harbor.mqtt_light import AppliedLampState, LampTopics, MqttFixtureLamp

COMMAND_TOPIC = "/entryplug_fixture_lamp/command"
STATE_TOPIC = "/entryplug_fixture_lamp/state"


class _WorldLamp(Node):
    """Only this process translates MQTT requests to the ROS fixture plugin."""

    def __init__(self, stop: threading.Event) -> None:
        super().__init__("entryplug_mqtt_fixture_lamp")
        self._stop = stop
        self.current: AppliedLampState | None = None
        self.command = self.create_publisher(Float32, COMMAND_TOPIC, 1)
        self.create_subscription(Float64MultiArray, STATE_TOPIC, self._on_state, 10)
        self.applied_count = 0

    def _on_state(self, message: Float64MultiArray) -> None:
        if len(message.data) != 3:
            return
        level, _sim_time, revision = message.data
        if not math.isfinite(level) or not math.isfinite(revision) or not 0 <= level <= 0.75:
            return
        if int(revision) != revision:
            return
        self.current = AppliedLampState(float(level), int(revision))

    def wait_until(self, condition: Callable[[], bool], label: str, timeout_s: float = 5.0) -> None:
        deadline = time.monotonic() + timeout_s
        while rclpy.ok() and not self._stop.is_set() and time.monotonic() < deadline:
            if condition():
                return
            rclpy.spin_once(self, timeout_sec=0.05)
        raise TimeoutError(f"fixture lamp {label} was not confirmed")

    def apply(self, level: float) -> AppliedLampState:
        if self.current is None:
            raise RuntimeError("initial lamp state is missing")
        self.wait_until(lambda: self.command.get_subscription_count() > 0, "subscriber")
        prior_revision = self.current.revision
        request = Float32()
        request.data = level
        self.command.publish(request)
        self.wait_until(
            lambda: (
                self.current is not None
                and self.current.revision > prior_revision
                and abs(self.current.level - level) <= 1 / 255
            ),
            "applied revision",
        )
        assert self.current is not None
        self.applied_count += 1
        return self.current


def run(
    run_id: str, broker_host: str, broker_port: int, stop: threading.Event
) -> dict[str, object]:
    rclpy.init(signal_handler_options=SignalHandlerOptions.NO)
    node = _WorldLamp(stop)
    lamp = MqttFixtureLamp(LampTopics(run_id), broker_host, broker_port)
    status = "stopped"
    try:
        node.wait_until(lambda: node.current is not None, "initial state", timeout_s=10.0)
        assert node.current is not None
        lamp.start()
        lamp.publish_initial_state(node.current)
        while rclpy.ok() and not stop.is_set():
            rclpy.spin_once(node, timeout_sec=0.05)
            lamp.process_one(node.apply)
    except Exception as error:
        status = "failed"
        reason = type(error).__name__
    else:
        reason = None
    try:
        lamp.close()
    except Exception as error:
        status = "failed"
        reason = reason or f"MQTT_CLOSE_{type(error).__name__}"
    finally:
        node.destroy_node()
        rclpy.try_shutdown()
    report: dict[str, object] = {
        "status": status,
        "applied_count": node.applied_count,
        "rejected_commands": lamp.rejected_commands,
    }
    if reason is not None:
        report["reason"] = reason
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--broker-host", required=True)
    parser.add_argument("--broker-port", type=int, default=1883)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    report = run(args.run_id, args.broker_host, args.broker_port, stop)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0 if report["status"] == "stopped" else 1


if __name__ == "__main__":
    raise SystemExit(main())
