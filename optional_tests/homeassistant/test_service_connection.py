import asyncio

import pytest
from test_light import ENTITY, FakeHomeAssistant, server

from entryplug_app.connections import Connections
from entryplug_app.service import ApplicationService
from entryplug_app.workspace import Workspace


@pytest.mark.asyncio
async def test_observed_device_report_loss_and_revalidation_reach_mission(tmp_path, monkeypatch):
    monkeypatch.setenv("ENTRYPLUG_HA_FIXTURE", "test-token")
    fake = FakeHomeAssistant()
    async with server(fake) as url:
        service = ApplicationService(Workspace.resolve(str(tmp_path)), demo=False)
        await service.open()
        connections = Connections(service, health_interval_s=0.02)

        async def wait_for(predicate):
            async with asyncio.timeout(3):
                while not await predicate():
                    await asyncio.sleep(0.01)

        try:
            connection = await connections.create(
                {
                    "kind": "homeassistant",
                    "name": "Fixture light",
                    "url": url,
                    "entity_id": ENTITY,
                    "token_env": "ENTRYPLUG_HA_FIXTURE",
                }
            )

            async def connected():
                return (await service.store.get("connections", connection["id"]))[
                    "status"
                ] == "connected"

            await wait_for(connected)
            body_id = connection["id"]
            first = (await service.topology())["nodes"][0]
            assert first["provenance"] == "observed" and first["simulated"] is False
            mission = await service.command(
                "mission.create",
                {
                    "name": "Read a light",
                    "instructions": "Read only",
                    "body": {
                        "selector": body_id,
                        "required_capabilities": ["homeassistant.light_state"],
                    },
                    "trigger": {"kind": "manual", "source": body_id},
                    "access": {"allow": ["homeassistant.light_state"]},
                },
                "mission",
            )
            run = await service.command("mission.start", {"mission_id": mission["id"]}, "start")
            fake.state["state"] = "unavailable"

            async def blocked():
                return (await service.store.get("runs", run["id"]))["health"] == "blocked"

            await wait_for(blocked)
            assert (await service.topology())["nodes"][0]["status"] == "lost"
            fake.state["state"] = "off"

            async def recovered():
                return (await service.store.get("runs", run["id"]))["health"] == "ok"

            await wait_for(recovered)
            after = (await service.topology())["nodes"][0]
            assert after["id"] == first["id"] and after["first_seen"] == first["first_seen"]
            assert after["last_seen"] != first["last_seen"]
            op = await service.command(
                "operation.create",
                {"run_id": run["id"], "capability": "homeassistant.light_state"},
                "read-state",
            )

            async def complete():
                return (await service.store.get("operations", op["id"]))["lifecycle"] == "succeeded"

            await wait_for(complete)
            result = await service.store.get("operations", op["id"])
            assert result["result"]["entity_id"] == ENTITY
            assert result["result"]["provenance"] == "device_report"
            assert result["evidence_ids"] and fake.commands == []
        finally:
            await service.close()
