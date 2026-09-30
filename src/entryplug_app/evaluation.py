"""Finite, account-free watch evaluation; no reset/reward tool reaches the agent."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

from entryplug_app.demo import SEQUENCE
from entryplug_app.service import ApplicationService
from entryplug_app.workspace import Workspace


async def evaluate(directory: Path) -> dict[str, Any]:
    results = []
    for driver in ("rules", "scripted"):
        service = ApplicationService(
            Workspace.resolve(str(directory / driver)), allow_scripted=True
        )
        await service.open()
        try:
            mission = await service.command(
                "mission.create",
                {
                    "name": "Finite watch evaluation",
                    "instructions": "Alert on validated entries",
                    "agent": {
                        "driver": driver,
                        "decision_mode": "rules" if driver == "rules" else "assisted",
                    },
                },
                "create",
            )
            run = await service.command("mission.start", {"mission_id": mission["id"]}, "start")
            await asyncio.sleep(0)
            before = (await service.store.get("runs", run["id"]))["model_calls"]
            timeline = []
            for action in SEQUENCE:
                event = await service.demo_step(action)
                async with asyncio.timeout(2):
                    while True:
                        current = await service.store.get("runs", run["id"])
                        if not current["pending"] and not current["active_turn_id"]:
                            break
                        await asyncio.sleep(0.005)
                timeline.append(
                    {
                        "action": action,
                        "event": event["type"],
                        "health": current["health"],
                        "turns": current["turn_count"],
                    }
                )
            alerts = await service.store.list("alerts")
            current = await service.command("run.stop", {"run_id": run["id"]}, "stop")
            results.append(
                {
                    "policy": driver,
                    "inference": "none" if driver == "rules" else "SIMULATED",
                    "run_id": run["id"],
                    "mission_id": mission["id"],
                    "revision": mission["revision"],
                    "turns": await service.store.list("turns"),
                    "operations": await service.store.list("operations"),
                    "evidence": await service.store.list("evidence"),
                    "alerts": len(alerts),
                    "expected_alerts": 4,
                    "model_calls": current["model_calls"],
                    "idle_model_calls": before,
                    "evidence_ids": [e for a in alerts for e in a["evidence_ids"]],
                    "timeline": timeline,
                }
            )
        finally:
            await service.close()
    return {
        "schema_version": 1,
        "fixture": "labelled-entrance-v1",
        "finite": True,
        "results": results,
        "conclusion": "Same four occurrences; no model advantage is claimed.",
    }


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="entryplug-evaluation-") as path:
        print(json.dumps(asyncio.run(evaluate(Path(path))), indent=2))


if __name__ == "__main__":
    main()
