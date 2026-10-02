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


@pytest.mark.asyncio
async def test_failed_connection_can_retry_and_be_probed_without_device_commands(
    tmp_path, monkeypatch
):
    import httpx

    from entryplug_server.app import create_app

    monkeypatch.delenv("ENTRYPLUG_HA_PROBE", raising=False)
    fake = FakeHomeAssistant()
    async with server(fake) as url:
        service = ApplicationService(Workspace.resolve(str(tmp_path)), demo=False)
        await service.open()
        app = create_app(service, manage_lifespan=False)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://127.0.0.1:8765",
                headers={"Authorization": "Bearer " + service.workspace.credential()},
            ) as client:
                configured = await client.post(
                    "/v1/connections",
                    json={
                        "kind": "homeassistant",
                        "name": "Probe fixture",
                        "url": url,
                        "entity_id": ENTITY,
                        "token_env": "ENTRYPLUG_HA_PROBE",
                    },
                    headers={"Idempotency-Key": "configure"},
                )
                assert configured.status_code == 202
                identifier = configured.json()["id"]

                async def wait_status(status):
                    async with asyncio.timeout(3):
                        while True:
                            current = await service.store.get("connections", identifier)
                            if current["status"] == status:
                                return current
                            await asyncio.sleep(0.01)

                failed = await wait_status("unverified")
                assert failed["reason_code"] == "credentials_missing"
                no_port = await client.post(
                    f"/v1/connections/{identifier}/test",
                    headers={"Idempotency-Key": "test-before-retry"},
                )
                assert no_port.status_code == 409
                monkeypatch.setenv("ENTRYPLUG_HA_PROBE", "test-token")
                retry = await client.post(
                    f"/v1/connections/{identifier}/retry",
                    headers={"Idempotency-Key": "retry"},
                )
                assert retry.status_code == 202
                await wait_status("connected")
                assert service.ports[identifier].connection_id == identifier
                healthy = await client.post(
                    f"/v1/connections/{identifier}/test",
                    headers={"Idempotency-Key": "healthy"},
                )
                assert healthy.json()["available"] is True
                assert healthy.json()["check"] == "read_only"
                fake.state["state"] = "unavailable"
                unavailable = await client.post(
                    f"/v1/connections/{identifier}/test",
                    headers={"Idempotency-Key": "unavailable"},
                )
                assert unavailable.json()["available"] is False
                assert fake.commands == []
                await client.delete(
                    f"/v1/connections/{identifier}",
                    headers={"Idempotency-Key": "disconnect"},
                )
                closed = await client.post(
                    f"/v1/connections/{identifier}/test",
                    headers={"Idempotency-Key": "test-after-disconnect"},
                )
                assert closed.status_code == 409
                assert (await service.store.get("connections", identifier))[
                    "status"
                ] == "disconnected"
                fake.state["state"] = "off"
                resumed = await client.post(
                    f"/v1/connections/{identifier}/retry",
                    headers={"Idempotency-Key": "retry-after-disconnect"},
                )
                assert resumed.status_code == 202
                await wait_status("connected")
                assert (await service.ports[identifier].health_check()).available is True
                assert fake.commands == []
        finally:
            await service.close()
