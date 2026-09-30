import httpx
import pytest

from entryplug_app.service import ApplicationService
from entryplug_app.workspace import Workspace
from entryplug_server.app import create_app
from entryplug_server.client import EntryplugClient


@pytest.mark.asyncio
async def test_simulation_http_controls_deduplicate_and_recording_supports_ranges(tmp_path):
    service = ApplicationService(Workspace.resolve(str(tmp_path)))
    await service.open()
    app = create_app(service, manage_lifespan=False)
    transport = httpx.ASGITransport(app=app)
    try:
        async with EntryplugClient(
            "http://127.0.0.1:8765", service.workspace.credential(), transport=transport
        ) as client:
            body = await client.request(
                "POST", "simulations", {"kind": "rover", "name": "Lab rover"}, key="one-body"
            )
            again = await client.request(
                "POST", "simulations", {"kind": "rover", "name": "Lab rover"}, key="one-body"
            )
            assert body["id"] == again["id"]
            states = await client.request("GET", "simulations")
            assert len(states) == 4
            await client.request(
                "POST",
                f"simulations/{body['id']}/control",
                {"action": "inject", "event": "obstacle"},
            )
            state = next(
                s for s in (await client.snapshot())["simulations"] if s["id"] == body["id"]
            )
            assert state["state"]["obstacle"] is True
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as raw:
            response = await raw.get("/assets/city-walk.webm", headers={"Range": "bytes=0-31"})
            assert response.status_code == 206
            assert response.headers["content-type"] == "video/webm"
            assert response.content[:4] == bytes.fromhex("1a45dfa3")
            assert len(response.content) == 32
            assert "content-encoding" not in response.headers
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_missing_login_extra_is_actionable_without_contacting_provider(tmp_path, monkeypatch):
    service = ApplicationService(Workspace.resolve(str(tmp_path)))
    await service.open()
    app = create_app(service, manage_lifespan=False)
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8765",
            headers={
                "Authorization": "Bearer " + service.workspace.credential(),
                "Idempotency-Key": "login",
            },
        ) as client:
            setup = (await client.get("/v1/providers")).json()
            assert not setup["openai_installed"] and "[ui,openai]" in setup["install_command"]
            login = await client.post("/v1/auth/chatgpt/start", json={"profile": "personal"})
            assert login.status_code == 503
            assert login.json()["code"] == "extra_required"
            assert "restart Entryplug" in login.json()["message"]
    finally:
        await service.close()
