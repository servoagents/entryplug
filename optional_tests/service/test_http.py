import asyncio

import httpx
import pytest

from entryplug_app.service import ApplicationService
from entryplug_app.workspace import Workspace
from entryplug_server.app import create_app
from entryplug_server.client import EntryplugClient


@pytest.mark.asyncio
async def test_client_parity_idempotency_security_and_bootstrap(tmp_path):
    service = ApplicationService(Workspace.resolve(str(tmp_path)))
    await service.open()
    app = create_app(service, manage_lifespan=False)
    transport = httpx.ASGITransport(app=app)
    token = service.workspace.credential()
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as raw:
            assert (await raw.get("/v1/health")).status_code == 401
            assert (await raw.get("/v1/health", headers={"Host": "evil.test"})).status_code == 403
            assert (
                await raw.get(
                    "/v1/health",
                    headers={"Authorization": "Bearer " + token, "Origin": "http://evil.test"},
                )
            ).status_code == 403
            bootstrap = app.state.security.bootstrap()
            exchange = await raw.post("/v1/session/exchange", json={"token": bootstrap})
            assert exchange.status_code == 200
            assert "HttpOnly" in exchange.headers["set-cookie"]
            assert (
                await raw.post("/v1/session/exchange", json={"token": bootstrap})
            ).status_code == 401
            assert (await raw.post("/v1/missions", json={})).status_code == 403
        async with EntryplugClient("http://127.0.0.1:8765", token, transport=transport) as client:
            mission = await client.create(
                {"name": "HTTP watch", "instructions": "Alert on entry"}, key="create"
            )
            run = await client.start(mission["id"], key="start")
            assert (await client.start(mission["id"], key="second-client"))["id"] == run["id"]
            await client.request("POST", "demo/step", {"action": "enter_a"})
            for _ in range(100):
                state = await client.snapshot()
                if state["alerts"] and state["runs"][0]["active_turn_id"] is None:
                    break
                await asyncio.sleep(0.01)
            assert state["runs"][0]["id"] == run["id"]
            assert state["alerts"][0]["run_id"] == run["id"]
            # Alert delivery precedes turn.finished; wait for that commit before
            # asserting which event follows the snapshot cursor.
            assert state["runs"][0]["active_turn_id"] is None
            cursor = state["cursor"]
            await client.request("POST", f"runs/{run['id']}/stop", {})
            events = await service.store.events(cursor)
            assert events[0]["type"] == "run.stop"
            external = await client.request(
                "POST",
                "attachments",
                {"name": "External", "body_id": "demo.access-camera", "allow": ["camera.snapshot"]},
            )
            async with EntryplugClient(
                "http://127.0.0.1:8765", external["token"], transport=transport
            ) as agent:
                assert (await agent.request("GET", "capabilities"))[0]["name"] == "camera.snapshot"
                op = await agent.request(
                    "POST",
                    "operations",
                    {"attachment_id": external["id"], "capability": "camera.snapshot"},
                    key="physical-request",
                )
                assert (
                    await agent.request(
                        "POST",
                        "operations",
                        {"attachment_id": external["id"], "capability": "camera.snapshot"},
                        key="physical-request",
                    )
                )["id"] == op["id"]
                response = await agent.http.get("/v1/snapshot")
                assert response.status_code == 403
            public = str(await client.snapshot()) + str(await service.store.events(0))
            assert external["token"] not in public
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_expired_cursor_is_http_410_before_stream(tmp_path):
    service = ApplicationService(Workspace.resolve(str(tmp_path)), event_retention=2)
    await service.open()
    try:
        app = create_app(service, manage_lifespan=False)
        for _ in range(4):
            await service.store.commit([], "test.event", {})
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8765",
            headers={"Authorization": "Bearer " + service.workspace.credential()},
        ) as client:
            response = await client.get("/v1/events?after=0")
            assert response.status_code == 410
            assert response.json()["code"] == "replay_expired"
    finally:
        await service.close()
