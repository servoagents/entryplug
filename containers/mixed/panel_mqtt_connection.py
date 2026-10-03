"""Late MQTT bridge join and loss through one persistent inspection Session."""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
from pathlib import Path

import rclpy
from panel_inspection import LocalPanelWorker, PanelFixture

from entryplug.core.evidence import json_object
from entryplug.core.operation import OperationHost
from entryplug.embodiment.inspection import CameraFrame, LightReport, inspection_spec
from entryplug.harness.session import Session
from entryplug_harbor.mqtt_control import MqttFixtureControl
from entryplug_harbor.mqtt_light import LampTopics


class MqttPanel:
    """Readiness and all task writes travel over MQTT; camera has no ROS writer."""

    def __init__(self, camera: PanelFixture, control: MqttFixtureControl) -> None:
        self.camera, self.control = camera, control
        self.entity_id = control.entity_id

    async def capture(self) -> CameraFrame:
        await self.control.read_state()
        return await self.camera.capture()

    async def set_brightness(self, level: float) -> LightReport:
        report = await self.control.set_brightness(level)
        state = self.control.applied[-1]
        assert state.sim_time_s is not None
        self.camera.minimum_sim_stamp = state.sim_time_s
        self.camera.applied_states.append(
            {
                "level": state.level,
                "revision": state.revision,
                "sim_time_s": state.sim_time_s,
            }
        )
        return report


async def stop_bridge(bridge: subprocess.Popen) -> None:
    bridge.terminate()
    try:
        await asyncio.to_thread(bridge.wait, timeout=8)
    except subprocess.TimeoutExpired:
        bridge.kill()
        await asyncio.to_thread(bridge.wait, timeout=3)
        raise RuntimeError("MQTT bridge required forced teardown") from None
    if bridge.returncode != 0:
        raise RuntimeError("MQTT bridge did not stop cleanly")


async def run(run_dir: Path, run_id: str) -> dict[str, object]:
    rclpy.init()
    camera = PanelFixture(run_dir, direct_lamp=False)
    worker = LocalPanelWorker()
    control = MqttFixtureControl(LampTopics(run_id), "broker")
    bridge = None
    operations = []
    try:
        await asyncio.to_thread(control.start)
        # Evaluator-only stale retained report, before any world bridge exists.
        receipt = control._client.publish(
            control.topics.state,
            b'{"state":"ON","brightness":191}',
            qos=1,
            retain=True,
        )
        await asyncio.to_thread(receipt.wait_for_publish, timeout=3)
        if not receipt.is_published():
            raise RuntimeError("stale retained state was not seeded")
        panel = MqttPanel(camera, control)
        host = OperationHost((inspection_spec(panel, worker, light=panel),), runtime_id=run_id)
        async with Session(host, owns_runtime=True) as session:

            async def inspect(label: str) -> None:
                op = await session.act(
                    "inspect_target", {"target_id": "bench-marker"}, request_id=label
                )
                await session.wait(op, 35)
                operations.append(json_object(await session.inspect(op, "result"), label))

            await inspect("retained-state-without-bridge")
            before_join = {
                "runtime_id": run_id,
                "operation": operations[-1],
                "commands_sent": len(control.commands),
                "read_replies": len(control.reads),
                "retained_state": {"state": "ON", "brightness": 191},
            }
            with (run_dir / "join-requested.json").open("x") as stream:
                json.dump(before_join, stream)
            with (run_dir / "bridge.log").open("x") as log:
                bridge = subprocess.Popen(
                    [
                        "/usr/bin/python3",
                        "/workspace/entryplug/containers/harbor/mqtt_lamp_bridge.py",
                        "--run-id",
                        run_id,
                        "--broker-host",
                        "broker",
                        "--output",
                        str(run_dir / "bridge.json"),
                    ],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
                for attempt in range(15):
                    try:
                        await control.read_state()
                        break
                    except TimeoutError:
                        if bridge.poll() is not None or attempt == 14:
                            raise
                        await asyncio.sleep(0.1)
                await inspect("after-bridge-join")
                await inspect("warm-mqtt")
                await stop_bridge(bridge)
                bridge = None
            # Retained bright state still exists, but no live challenge response.
            await inspect("retained-state-after-bridge-stop")
        return {
            "status": "completed",
            "runtime_id": run_id,
            "operations": operations,
            "applied_states": camera.applied_states,
            "frames": camera.frames,
            "frame_artifacts": camera.frame_artifacts,
            "mqtt_commands": control.commands,
            "mqtt_reads": control.reads,
            "bridge_join": before_join,
            "detector": "local-panel-worker-v1",
        }
    finally:
        try:
            if bridge is not None and bridge.poll() is None:
                await stop_bridge(bridge)
        finally:
            await asyncio.to_thread(control.close)
            worker.close()
            camera.destroy_node()
            rclpy.shutdown()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    try:
        report = asyncio.run(run(args.run_dir, args.run_id))
    except Exception as error:
        report = {"status": "failed", "reason": type(error).__name__, "detail": str(error)}
    with (args.run_dir / "mqtt.json").open("x") as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
        stream.write("\n")
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
