"""Owned fixture: abrupt owner death after lamp application, then fresh-host recovery."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

from hybrid_runtime import open_hybrid_runtime

from entryplug_app.contracts import AppError, utc_now
from entryplug_app.inspection import inspection_mission
from entryplug_app.ports import EmbodimentPort
from entryplug_app.service import ApplicationService
from entryplug_app.workspace import Workspace


async def recover(root: Path, run_id: str, token_file: Path) -> dict[str, object]:
    checkpoint = json.loads((root / "before/crash.json").read_text())
    async with open_hybrid_runtime(
        root / "after", run_id, token_file, runtime_id=run_id + "-recovered"
    ) as runtime:
        view = await runtime.session.observe()
        service = ApplicationService(
            Workspace.resolve(str(root / "before/mission-workspace")),
            ports=[
                EmbodimentPort(
                    "borrowed-light",
                    "Borrowed Light",
                    runtime.session,
                    simulated=True,
                    provenance="native_protocols_simulated_devices",
                )
            ],
            demo=False,
        )
        await service.open()
        try:
            operations = await service.store.list("operations")
            if len(operations) != 1:
                raise RuntimeError("expected exactly one recovered intent")
            op = operations[0]
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
            mission = await service.command(
                "mission.create", inspection_mission("borrowed-light", approve=False), "new-mission"
            )
            attachment = await service.command(
                "attachment.create",
                {"name": "Retry check", "body_id": "borrowed-light", "allow": ["inspect_target"]},
                "retry-attachment",
            )
            refusals = {}
            for label, action, payload in [
                ("resume", "run.resume", {"run_id": op["run_id"]}),
                ("start", "mission.start", {"mission_id": mission["id"]}),
                (
                    "write",
                    "operation.create",
                    {
                        "attachment_id": attachment["id"],
                        "capability": op["capability"],
                        "arguments": op["arguments"],
                    },
                ),
            ]:
                try:
                    await service.command(action, payload, "recovered-" + label)
                except AppError as error:
                    refusals[label] = error.code
            await service.ingest(
                {
                    "source": "borrowed-light",
                    "type": "source.revalidated",
                    "epoch": "recovered",
                    "cursor": 1,
                    "at": utc_now(),
                    "occurrence": "reconnected",
                }
            )
            # Give any incorrectly scheduled work a chance to reach the new host.
            await asyncio.sleep(0.1)
            final = await runtime.session.observe()
            return {
                "status": "completed",
                "checkpoint": checkpoint,
                "recovery_pid": os.getpid(),
                "fresh_runtime_id": view.runtime_id,
                "fresh_runtime_operations": len(view.operations),
                "fresh_runtime_inhibition": view.effect_inhibited_reason,
                "final_runtime_operations": len(final.operations),
                "duplicate_application_id": duplicate["id"],
                "refusals": refusals,
                "application_operations": await service.store.list("operations"),
                "mission_run": await service.store.get("runs", op["run_id"]),
                "turns": await service.store.list("turns"),
                "home_assistant_entity": runtime.light.entity_id,
            }
        finally:
            await service.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--token-file", type=Path, default=Path("/run/secrets/ha-token"))
    args = parser.parse_args()
    root = args.output.parent
    (root / "before").mkdir()
    (root / "after").mkdir()
    with (root / "before/owner.log").open("x") as log:
        child = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).with_name("hybrid_mission.py")),
                "--output",
                str(root / "before/hybrid.json"),
                "--run-id",
                args.run_id,
                "--token-file",
                str(args.token_file),
                "--crash-after-apply",
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=80,
        )
    if child.returncode != -signal.SIGKILL:
        raise RuntimeError(f"fixture owner did not reach the declared crash: {child.returncode}")
    report = asyncio.run(recover(root, args.run_id, args.token_file))
    report["owner_exit_code"] = child.returncode
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")


if __name__ == "__main__":
    main()
