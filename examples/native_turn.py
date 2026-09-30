"""Explicit opt-in real inference against a read-only Session capability."""

import asyncio
import json
import os

from entryplug_server.client import EntryplugClient


async def main():
    if os.environ.get("ENTRYPLUG_LIVE_INFERENCE") != "1":
        raise SystemExit("Set ENTRYPLUG_LIVE_INFERENCE=1 to authorize real provider usage")
    profile = os.environ["ENTRYPLUG_PROFILE"]
    body = os.environ.get("ENTRYPLUG_BODY", "demo.access-camera")
    capability = os.environ.get("ENTRYPLUG_CAPABILITY", "camera.snapshot")
    if capability not in {"camera.snapshot", "homeassistant.light_state"}:
        raise SystemExit(
            "This smoke test only admits the known read-only fixture/device capabilities"
        )
    async with EntryplugClient.from_workspace(
        os.environ.get("ENTRYPLUG_WORKSPACE", "default")
    ) as client:
        if body == "demo.access-camera":
            await client.request("POST", "demo/step", {"action": "idle"})
        mission = await client.create(
            {
                "name": "Opt-in native read",
                "mode": "once",
                "instructions": (
                    "Use the granted read tool once. Describe what its evidence supports, "
                    "including simulation and uncertainty. Do not infer an entry "
                    "from a single presence observation."
                ),
                "agent": {"driver": "native", "decision_mode": "assisted", "profile": profile},
                "body": {"selector": body, "required_capabilities": [capability]},
                "trigger": {"kind": "manual", "source": body},
                "access": {"allow": [capability]},
            }
        )
        run = await client.start(mission["mission_id"])
        for _ in range(100):
            state = await client.request("GET", "runs/" + run["run_id"])
            if state["lifecycle"] in {"completed", "failed"} or state["health"] == "blocked":
                print(json.dumps(state, indent=2))
                return
            await asyncio.sleep(0.5)
        raise SystemExit("Turn did not finish within the smoke-test window; inspect the run")


asyncio.run(main())
