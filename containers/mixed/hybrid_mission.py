"""Owned development fixture: run the native body through resident mission authority."""

from __future__ import annotations

import argparse
import asyncio
import json
from contextlib import AsyncExitStack
from pathlib import Path

from ha_reply_loss import lose_service_reply
from hybrid_ports import HA_URL
from hybrid_runtime import open_hybrid_runtime

from entryplug.core.evidence import json_object
from entryplug.core.operation import AdmissionError
from entryplug_app.contracts import AppError
from entryplug_app.inspection import inspection_mission
from entryplug_app.ports import EmbodimentPort
from entryplug_app.service import ApplicationService
from entryplug_app.workspace import Workspace


async def run(
    output: Path, run_id: str, token_file: Path, *, lost_reply: bool = False
) -> dict[str, object]:
    async with AsyncExitStack() as stack:
        fault: dict[str, object] = {}

        async def observe_applied() -> dict[str, object]:
            # Evaluator-only read of public ROS feedback. The task is waiting
            # for HA here, so no other code spins this camera node concurrently.
            camera = runtime.camera
            assert camera.latest_state is not None
            prior_revision = camera.latest_state[2]
            await asyncio.to_thread(
                camera._spin_until,
                lambda: camera.latest_state is not None
                and camera.latest_state[2] > prior_revision
                and abs(camera.latest_state[0] - 64 / 255) < 0.01,
                "applied state before lost HA result",
                3.0,
            )
            level, stamp, revision = camera.latest_state
            return {
                "level": level,
                "sim_time_s": stamp,
                "revision": revision,
                "prior_revision": prior_revision,
            }

        url = HA_URL
        if lost_reply:
            url, fault = await stack.enter_async_context(
                lose_service_reply(HA_URL, observe_applied)
            )
        runtime = await stack.enter_async_context(
            open_hybrid_runtime(output.parent, run_id, token_file, home_assistant_url=url)
        )
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
            if lost_reply and len(application_operations) == 1:
                op = application_operations[0]
                duplicate = await service.command(
                    "operation.create",
                    {
                        "run_id": op["run_id"],
                        "turn_id": op["turn_id"],
                        "capability": op["capability"],
                        "arguments": op["arguments"],
                    },
                    f"{op['turn_id']}:1",
                )
                fault["duplicate_application_id"] = duplicate["id"]
                duplicate_native = await runtime.session.act(
                    op["capability"], op["arguments"], request_id=op["request_id"]
                )
                fault["duplicate_native_id"] = duplicate_native.operation_id
                try:
                    await service.command(
                        "operation.create",
                        {
                            "run_id": op["run_id"],
                            "capability": op["capability"],
                            "arguments": op["arguments"],
                        },
                        "fresh-retry",
                    )
                except AppError as error:
                    fault["application_retry_refusal"] = error.code
                try:
                    await runtime.session.act(
                        op["capability"], op["arguments"], request_id="fresh-retry"
                    )
                except AdmissionError as error:
                    fault["native_retry_refusal"] = error.reason_code
                fault["effect_inhibited_reason"] = (
                    await runtime.session.observe()
                ).effect_inhibited_reason
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
                "fault": fault,
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
    parser.add_argument("--lost-reply", action="store_true")
    args = parser.parse_args()
    report = asyncio.run(run(args.output, args.run_id, args.token_file, lost_reply=args.lost_reply))
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")


if __name__ == "__main__":
    main()
