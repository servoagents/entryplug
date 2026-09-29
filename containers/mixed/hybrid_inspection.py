#!/usr/bin/python3
"""One continuing panel task through HA lighting and a native Zenoh worker."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

import rclpy
import zenoh
from panel_inspection import PanelFixture

from entryplug.core.evidence import json_object
from entryplug.core.operation import Lifecycle, OperationHost
from entryplug.embodiment.inspection import LightReport, inspection_spec
from entryplug.harness.session import Session
from entryplug_homeassistant.light import HomeAssistantLight, HomeAssistantLightError
from entryplug_homeassistant.registry import resolve_mqtt_light
from entryplug_zenoh.detector import ZenohDetectorWorker

HA_URL = "ws://homeassistant:8123/api/websocket"


def _zenoh_config() -> zenoh.Config:
    return zenoh.Config.from_json5(
        json.dumps(
            {
                "mode": "client",
                "scouting": {"multicast": {"enabled": False}},
                "connect": {"endpoints": ["tcp/native-router:7448"]},
            }
        )
    )


class AppliedHAPanelLight:
    """HA is the only write port; ROS feedback supplies the image-time barrier."""

    def __init__(self, camera: PanelFixture, adapter: HomeAssistantLight) -> None:
        self.entity_id = adapter.entity_id
        self._camera = camera
        self._adapter = adapter

    async def set_brightness(self, level: float) -> LightReport:
        camera = self._camera
        await asyncio.to_thread(
            camera._spin_until,
            lambda: camera.latest_state is not None,
            "initial applied lamp state",
            8.0,
        )
        assert camera.latest_state is not None
        prior_revision = camera.latest_state[2]
        report = await self._adapter.set_brightness(level)

        def applied() -> bool:
            state = camera.latest_state
            return state is not None and state[2] > prior_revision and abs(state[0] - level) <= 0.01

        await asyncio.to_thread(camera._spin_until, applied, "new applied lamp revision", 8.0)
        assert camera.latest_state is not None
        applied_level, sim_time, revision = camera.latest_state
        camera.minimum_sim_stamp = sim_time
        camera.applied_states.append(
            {"level": applied_level, "sim_time_s": sim_time, "revision": revision}
        )
        return report


async def _connect_light(token: str, run_id: str) -> HomeAssistantLight:
    unique_id = f"entryplug_fixture_{run_id}"
    deadline = time.monotonic() + 20
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            entity_id = await resolve_mqtt_light(
                HA_URL, token, unique_id, allow_insecure_network=True
            )
            adapter = HomeAssistantLight(HA_URL, token, entity_id, allow_insecure_network=True)
            ready = False
            try:
                await adapter.connect()
                if (await adapter.current_state()).available:
                    ready = True
                    return adapter
            finally:
                if not ready:
                    await adapter.close()
        except HomeAssistantLightError as error:
            last_error = error
        await asyncio.sleep(0.5)
    raise HomeAssistantLightError("fixture MQTT light did not become available") from last_error


async def run(output: Path, run_id: str, token_file: Path) -> dict[str, object]:
    token = token_file.read_text(encoding="utf-8").strip()
    rclpy.init()
    camera = PanelFixture(output.parent, direct_lamp=False)
    adapter: HomeAssistantLight | None = None
    try:
        adapter = await _connect_light(token, run_id)
        with zenoh.open(_zenoh_config()) as native_session:
            worker = ZenohDetectorWorker(
                native_session, run_id=run_id, worker_id="worker-a", generation=1
            )
            host = OperationHost(
                (inspection_spec(camera, worker, light=AppliedHAPanelLight(camera, adapter)),),
                runtime_id=run_id,
            )
            session = Session(host, owns_runtime=True)
            try:
                operations: list[dict[str, object]] = []
                for request_id in (f"{run_id}-first", f"{run_id}-warm"):
                    operation = await session.act(
                        "inspect_target", {"target_id": "bench-marker"}, request_id=request_id
                    )
                    result = await session.wait(operation, 35.0)
                    record = json_object(
                        await session.inspect(operation.operation_id, detail="result"),
                        "hybrid panel inspection",
                    )
                    operations.append(record)
                    if result.lifecycle != Lifecycle.SUCCEEDED:
                        break
                first = operations[0].get("result") if operations else None
                warm = operations[1].get("result") if len(operations) == 2 else None
                passed = (
                    isinstance(first, dict)
                    and isinstance(warm, dict)
                    and all(record.get("lifecycle") == "succeeded" for record in operations)
                    and operations[0].get("operation_id") != operations[1].get("operation_id")
                    and first.get("worker_id") == warm.get("worker_id") == "worker-a"
                    and first.get("lighting_writes") == 3
                    and warm.get("lighting_writes") == 0
                    and not set(first.get("sample_ids", [])) & set(warm.get("sample_ids", []))
                )
                return {
                    "status": "passed" if passed else "failed",
                    "transport": "HA WebSocket -> MQTT -> ROS/MuJoCo -> camera -> native Zenoh",
                    "home_assistant_entity": adapter.entity_id,
                    "operations": operations,
                    "applied_states": camera.applied_states,
                    "frame_artifacts": camera.frame_artifacts,
                }
            finally:
                await session.close()
                worker.close()
    finally:
        if adapter is not None:
            await adapter.close()
        camera.destroy_node()
        rclpy.shutdown()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--token-file", type=Path, default=Path("/run/secrets/ha-token"))
    args = parser.parse_args()
    try:
        report = asyncio.run(run(args.output, args.run_id, args.token_file))
    except Exception as error:
        report = {"status": "failed", "reason": type(error).__name__}
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
