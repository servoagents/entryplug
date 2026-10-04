"""Own the live hybrid resources behind one task-level Session."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import rclpy
import zenoh
from hybrid_ports import HA_URL, AppliedHAPanelLight, connect_light, zenoh_config
from panel_inspection import PanelFixture

from entryplug.core.operation import OperationHost
from entryplug.embodiment.inspection import inspection_spec
from entryplug.harness.session import Session
from entryplug_homeassistant.light import HomeAssistantLight
from entryplug_zenoh.detector import ZenohDetectorWorker


@dataclass(slots=True)
class HybridRuntime:
    session: Session
    camera: PanelFixture
    light: HomeAssistantLight


@asynccontextmanager
async def open_hybrid_runtime(
    run_dir: Path,
    run_id: str,
    token_file: Path,
    *,
    home_assistant_url: str = HA_URL,
    runtime_id: str | None = None,
) -> AsyncIterator[HybridRuntime]:
    """Keep ROS, HA, native compute and the operation ledger alive together."""

    token = token_file.read_text(encoding="utf-8").strip()
    rclpy.init()
    camera = PanelFixture(run_dir, direct_lamp=False)
    light: HomeAssistantLight | None = None
    try:
        light = await connect_light(token, run_id, url=home_assistant_url)
        with zenoh.open(zenoh_config()) as native_session:
            worker = ZenohDetectorWorker(
                native_session, run_id=run_id, worker_id="worker-a", generation=1
            )
            alternate = (
                ZenohDetectorWorker(
                    native_session, run_id=run_id, worker_id="worker-b", generation=1
                )
                if os.environ.get("ENTRYPLUG_HYBRID_ALTERNATE") == "1"
                else None
            )
            host = OperationHost(
                (
                    inspection_spec(
                        camera,
                        worker,
                        alternate_worker=alternate,
                        light=AppliedHAPanelLight(camera, light),
                    ),
                ),
                runtime_id=runtime_id or run_id,
            )
            session = Session(host, owns_runtime=True)
            try:
                yield HybridRuntime(session, camera, light)
            finally:
                await session.close()
                worker.close()
                if alternate is not None:
                    alternate.close()
    finally:
        if light is not None:
            await light.close()
        camera.destroy_node()
        rclpy.shutdown()
