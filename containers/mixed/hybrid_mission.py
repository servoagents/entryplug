"""Owned development fixture: run the native body through resident mission authority."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from hybrid_runtime import open_hybrid_runtime

from entryplug.core.evidence import json_object
from entryplug_app.inspection import inspection_mission
from entryplug_app.ports import EmbodimentPort
from entryplug_app.service import ApplicationService
from entryplug_app.workspace import Workspace


async def run(output: Path, run_id: str, token_file: Path) -> dict[str, object]:
    async with open_hybrid_runtime(output.parent, run_id, token_file) as runtime:
        port = EmbodimentPort(
            "borrowed-light",
            "Borrowed Light development fixture",
            runtime.session,
            simulated=True,
            provenance="native_protocols_simulated_devices",
        )
        service = ApplicationService(
            Workspace.resolve(str(output.parent / "mission-workspace")), ports=[port], demo=False
        )
        await service.open()
        try:
            # Explicit grant is limited to this owned disposable fixture. The
            # reusable mission template requires per-operation approval by default.
            mission = await service.command(
                "mission.create", inspection_mission(port.body_id, approve=False), "create"
            )
            runs = []
            for index in range(2):
                current = await service.command(
                    "mission.start", {"mission_id": mission["id"]}, f"start-{index}"
                )
                async with asyncio.timeout(50):
                    while not current["turn_count"]:
                        await asyncio.sleep(0.02)
                        current = await service.store.get("runs", current["id"])
                runs.append(current)
                if current["lifecycle"] != "completed":
                    break
            application_operations = await service.store.list("operations")
            operations = [
                json_object(
                    await runtime.session.inspect(op["native_operation_id"], "result"),
                    "native inspection",
                )
                for op in application_operations
                if op.get("native_operation_id")
            ]
            return {
                "status": "completed",
                "transport": "HA WebSocket -> MQTT -> ROS/MuJoCo -> camera -> native Zenoh",
                "home_assistant_entity": runtime.light.entity_id,
                "operations": operations,
                "applied_states": runtime.camera.applied_states,
                "frame_artifacts": runtime.camera.frame_artifacts,
                "frames": runtime.camera.frames,
                "mission": mission,
                "mission_runs": runs,
                "application_operations": application_operations,
                "mission_evidence": await service.store.list("evidence"),
            }
        finally:
            await service.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--token-file", type=Path, default=Path("/run/secrets/ha-token"))
    args = parser.parse_args()
    report = asyncio.run(run(args.output, args.run_id, args.token_file))
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")


if __name__ == "__main__":
    main()
