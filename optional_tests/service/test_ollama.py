"""Real HTTP client and bounded agent loop against a local provider protocol fixture."""

import asyncio
import json

import httpx
import pytest

from entryplug_agents.auth import ProviderProfiles
from entryplug_agents.ollama_backend import OllamaBackend, check_model, local_models
from entryplug_app.contracts import AppError, validate_definition
from entryplug_app.drivers import TurnContext
from entryplug_app.service import ApplicationService
from entryplug_app.workspace import Workspace


def transport(monkeypatch, respond):
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(respond), **kw)
    )


@pytest.mark.asyncio
async def test_local_profile_tools_and_result_ids_survive_the_bounded_loop(tmp_path, monkeypatch):
    service = ApplicationService(Workspace.resolve(str(tmp_path)))
    await service.open()
    profiles = ProviderProfiles(service, "http://127.0.0.1:8765")
    await profiles.initialize()
    calls = []

    def respond(request):
        assert request.url.host == "127.0.0.1" and request.url.port == 11434
        assert "authorization" not in request.headers
        body = json.loads(request.content)
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": ["completion", "tools"]})
        calls.append(body)
        assert body["model"] == "local-test" and body["stream"] is False
        if len(calls) == 1:
            assert body["tools"][0]["name"] == "room__observe"
            output = [
                {
                    "type": "function_call",
                    "name": "room__observe",
                    "arguments": "{}",
                    "call_id": "read-1",
                }
            ]
        else:
            assert body["input"][-1] == {
                "type": "function_call_output",
                "call_id": "read-1",
                "output": '{"occupied":true}',
            }
            output = [
                {"type": "message", "content": [{"type": "output_text", "text": "Room observed."}]}
            ]
        return httpx.Response(
            200, json={"status": "completed", "output": output, "usage": {"input_tokens": 12}}
        )

    transport(monkeypatch, respond)

    class Tools:
        async def catalog(self):
            return [
                {
                    "name": "room.observe",
                    "description": "Read room",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                }
            ]

        async def call(self, name, arguments):
            assert name == "room.observe" and arguments == {}
            return {"occupied": True}

    try:
        saved = await profiles.configure(
            {"id": "local", "provider": "ollama", "model": "local-test"}
        )
        assert saved["status"] == "ready" and saved["live_validation"] == "not_run"
        context = TurnContext(
            "run",
            "turn",
            validate_definition({"name": "Test", "instructions": "Observe"}),
            {},
            [],
            Tools(),
        )
        assert await service.drivers["local"].turn(context) == "Room observed."
        assert context.model_calls == 2 and context.usage[0]["input_tokens"] == 12
        restored = ProviderProfiles(service, "http://127.0.0.1:8765")
        await restored.initialize()
        assert restored.public()[0]["provider"] == "ollama"
        await restored.logout("local")
        assert "local" not in service.drivers
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response,code",
    [
        ({"capabilities": ["completion"]}, "model_tools_required"),
        ({"capabilities": ["tools"], "remote_host": "https://ollama.com"}, "provider_unsupported"),
    ],
)
async def test_local_model_requires_tools_and_does_not_select_cloud(monkeypatch, response, code):
    transport(monkeypatch, lambda r: httpx.Response(200, json=response))
    with pytest.raises(AppError) as error:
        await check_model("candidate")
    assert error.value.code == code


@pytest.mark.asyncio
async def test_model_list_excludes_cloud_entries(monkeypatch):
    transport(
        monkeypatch,
        lambda r: httpx.Response(
            200, json={"models": [{"name": "local:small"}, {"name": "remote:cloud"}]}
        ),
    )
    assert await local_models() == [{"id": "local:small", "name": "local:small"}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply",
    [
        {"status": "incomplete", "output": []},
        {
            "status": "completed",
            "output": [{"type": "function_call", "name": "room__observe", "arguments": "{}"}],
        },
    ],
)
async def test_partial_model_response_never_yields_actions(monkeypatch, reply):
    transport(monkeypatch, lambda r: httpx.Response(200, json=reply))
    with pytest.raises(AppError, match="Ollama"):
        await OllamaBackend("local").infer("observe", [], [])


@pytest.mark.asyncio
async def test_unreachable_local_server_has_actionable_error(monkeypatch):
    def unavailable(request):
        raise httpx.ConnectError("offline", request=request)

    transport(monkeypatch, unavailable)
    with pytest.raises(AppError, match="Start it on 127.0.0.1:11434") as error:
        await local_models()
    assert error.value.code == "provider_unavailable"


@pytest.mark.asyncio
async def test_local_model_actions_move_the_rover_through_the_mission_broker(tmp_path, monkeypatch):
    service = ApplicationService(Workspace.resolve(str(tmp_path)))
    await service.open()
    profiles = ProviderProfiles(service, "http://127.0.0.1:8765")
    await profiles.initialize()
    replies = 0

    def respond(request):
        nonlocal replies
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": ["tools"]})
        body = json.loads(request.content)
        replies += 1
        assert {t["name"] for t in body["tools"]} == {"robot__observe", "robot__move"}
        if replies <= 2:
            if replies == 2:
                result = body["input"][-1]
                assert result["call_id"] == "step-1"
                assert "dock" in result["output"]
            output = [
                {
                    "type": "function_call",
                    "call_id": f"step-{replies}",
                    "name": "robot__observe" if replies == 1 else "robot__move",
                    "arguments": "{}" if replies == 1 else '{"waypoint":"aisle"}',
                }
            ]
        else:
            assert body["input"][-1]["call_id"] == "step-2"
            assert "aisle" in body["input"][-1]["output"]
            output = [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Reached the aisle; waiting for the next event.",
                        }
                    ],
                }
            ]
        return httpx.Response(200, json={"status": "completed", "output": output})

    transport(monkeypatch, respond)
    try:
        await profiles.configure({"id": "local", "provider": "ollama", "model": "local-test"})
        mission = await service.command(
            "mission.create",
            {
                "name": "Local model rover",
                "instructions": "Move one waypoint per event",
                "agent": {"driver": "native", "profile": "local", "decision_mode": "assisted"},
                "body": {"selector": "demo.rover", "required_capabilities": ["robot.observe"]},
                "trigger": {"source": "demo.rover", "event": "simulation.changed"},
                "access": {"allow": ["robot.observe", "robot.move"]},
            },
            "create",
        )
        run = await service.command("mission.start", {"mission_id": mission["id"]}, "start")
        await service.simulations.control("demo.rover", {"action": "step"})
        async with asyncio.timeout(5):
            while True:
                current = await service.store.get("runs", run["id"])
                if current["turn_count"] and not current["active_turn_id"]:
                    break
                await asyncio.sleep(0.01)
        assert current["health"] == "ok" and current["model_calls"] == 3
        assert current["last_result"] == "Reached the aisle; waiting for the next event."
        sim = service.simulations.describe("demo.rover")
        assert sim["state"]["waypoint"] == "aisle" and not sim["playing"]
        assert sim["remaining_events"] == 3
        operations = await service.store.list("operations")
        assert {op["capability"] for op in operations} == {"robot.observe", "robot.move"}
        assert all(op["lifecycle"] == "succeeded" and op["evidence_ids"] for op in operations)
    finally:
        await service.close()
