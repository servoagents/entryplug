#!/usr/bin/python3
"""Evaluator-only proof of broker -> ROS lamp -> rendered panel RGB."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import rclpy
from lamp_spike import LampSpike, evaluate_visibility
from paho.mqtt import client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

from entryplug_harbor.mqtt_light import LampTopics


def _wait_for(
    check: Callable[[], bool], node: LampSpike, label: str, timeout_s: float = 15.0
) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if check():
            return
        rclpy.spin_once(node, timeout_sec=0.05)
    raise TimeoutError(f"timed out waiting for {label}")


def run(output: Path, run_id: str, broker_host: str, broker_port: int) -> dict[str, object]:
    topics = LampTopics(run_id)
    rclpy.init()
    node = LampSpike()
    seen: list[tuple[str, bytes]] = []
    listener = mqtt.Client(CallbackAPIVersion.VERSION2, client_id=f"evaluator-{uuid.uuid4().hex}")
    listener.on_message = lambda _client, _userdata, message: seen.append(
        (message.topic, message.payload)
    )
    bridge: subprocess.Popen[str] | None = None
    qualified = False
    try:
        dark_time, dark_revision = node.applied_state(0.0)
        dark = node.captures_after(dark_time, "mqtt-dark", output.parent)
        listener.connect(broker_host, broker_port, keepalive=15)
        listener.loop_start()
        listener.subscribe([(topics.discovery, 0), (topics.state, 0)])

        with (output.parent / "mqtt-bridge.log").open("x", encoding="utf-8") as log:
            bridge = subprocess.Popen(
                [
                    "/usr/bin/python3",
                    "/workspace/entryplug/containers/harbor/mqtt_lamp_bridge.py",
                    "--run-id",
                    run_id,
                    "--broker-host",
                    broker_host,
                    "--broker-port",
                    str(broker_port),
                    "--output",
                    str(output.parent / "mqtt-bridge.json"),
                ],
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
            )
            _wait_for(
                lambda: (
                    any(topic == topics.discovery and payload for topic, payload in seen)
                    and any(
                        topic == topics.state and json.loads(payload).get("state") == "OFF"
                        for topic, payload in seen
                    )
                ),
                node,
                "discovery and initial applied state",
            )
            before_on = sum(
                topic == topics.state and json.loads(payload).get("state") == "ON"
                for topic, payload in seen
            )
            brightness = 191
            desired_level = brightness / 255
            receipt = listener.publish(
                topics.command,
                json.dumps({"state": "ON", "brightness": brightness}).encode(),
                qos=0,
                retain=False,
            )
            receipt.wait_for_publish(timeout=3.0)
            bright_time, bright_revision = node.applied_state(desired_level)
            _wait_for(
                lambda: (
                    sum(
                        topic == topics.state and json.loads(payload).get("state") == "ON"
                        for topic, payload in seen
                    )
                    > before_on
                ),
                node,
                "post-apply MQTT state",
            )
            bright = node.captures_after(bright_time, "mqtt-bright", output.parent)
            passed, reason, delta = evaluate_visibility(dark, bright)
            if bright_revision <= dark_revision or bright_time <= dark_time:
                passed, reason = False, "LAMP_APPLY_BOUNDARY_NOT_PROGRESSING"
            qualified = True
            return {
                "status": "passed" if passed else "failed",
                "reason": reason,
                "fixture": "stationary marked panel, no joints or arm commands",
                "transport": "MQTT broker -> queued ROS lamp command -> applied MQTT state",
                "dark_revision": dark_revision,
                "bright_revision": bright_revision,
                "dark_applied_sim_time": dark_time,
                "bright_applied_sim_time": bright_time,
                "dark": dark,
                "bright": bright,
                "mean_intensity_delta": delta,
                "mqtt_command_brightness": brightness,
                "mqtt_reported_state": [
                    json.loads(payload)
                    for topic, payload in seen
                    if topic == topics.state and payload
                ],
            }
    finally:
        if bridge is not None:
            bridge.terminate()
            try:
                bridge.wait(timeout=8.0)
            except subprocess.TimeoutExpired:
                bridge.kill()
                bridge.wait(timeout=3.0)
        listener.disconnect()
        listener.loop_stop()
        node.detector.close()
        node.destroy_node()
        rclpy.shutdown()
        if qualified and bridge is not None:
            bridge_path = output.parent / "mqtt-bridge.json"
            if bridge.returncode != 0 or not bridge_path.is_file():
                raise RuntimeError("MQTT bridge did not finish and retain its applied-state report")
            bridge_report = json.loads(bridge_path.read_text(encoding="utf-8"))
            if bridge_report.get("status") != "stopped" or bridge_report.get("applied_count") != 1:
                raise RuntimeError("MQTT bridge did not confirm exactly one applied command")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--broker-host", required=True)
    parser.add_argument("--broker-port", type=int, default=1883)
    args = parser.parse_args()
    try:
        report = run(args.output, args.run_id, args.broker_host, args.broker_port)
    except Exception as error:
        report = {"status": "failed", "reason": type(error).__name__, "detail": str(error)}
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
