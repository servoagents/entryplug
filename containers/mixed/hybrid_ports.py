"""Qualified HA and native-Zenoh ports for the hybrid panel fixture."""

from __future__ import annotations

import asyncio
import json
import time

import zenoh
from panel_inspection import PanelFixture

from entryplug.embodiment.inspection import LightReport
from entryplug_homeassistant.light import HomeAssistantLight, HomeAssistantLightError
from entryplug_homeassistant.registry import resolve_mqtt_light

HA_URL = "ws://homeassistant:8123/api/websocket"


def zenoh_config() -> zenoh.Config:
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

        await asyncio.to_thread(
            camera._spin_until, applied, "new applied lamp revision", 8.0
        )
        assert camera.latest_state is not None
        applied_level, sim_time, revision = camera.latest_state
        camera.minimum_sim_stamp = sim_time
        camera.applied_states.append(
            {"level": applied_level, "sim_time_s": sim_time, "revision": revision}
        )
        return report


async def connect_light(token: str, run_id: str) -> HomeAssistantLight:
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
