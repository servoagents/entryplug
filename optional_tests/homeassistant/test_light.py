"""Local WebSocket protocol tests, not an upstream Home Assistant qualification."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from aiohttp import web

from entryplug.core.operation import EffectState, Lifecycle, OperationHost
from entryplug.embodiment.inspection import (
    CameraFrame,
    MarkerMeasurement,
    inspection_spec,
)
from entryplug.harness.session import Session
from entryplug_homeassistant import HomeAssistantLight, HomeAssistantLightError

ENTITY = "light.fixture_panel"


class FakeHomeAssistant:
    def __init__(
        self, *, fail_auth: bool = False, lose_service: bool = False, report_stale: bool = False
    ) -> None:
        self.fail_auth = fail_auth
        self.lose_service = lose_service
        self.report_stale = report_stale
        self.connections = 0
        self.commands: list[dict[str, object]] = []
        self.state: dict[str, object] = {
            "entity_id": ENTITY,
            "state": "off",
            "attributes": {"supported_color_modes": ["brightness"]},
        }

    async def websocket(self, request: web.Request) -> web.WebSocketResponse:
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        self.connections += 1
        await socket.send_json({"type": "auth_required"})
        auth = json.loads((await socket.receive()).data)
        assert auth == {"type": "auth", "access_token": "test-token"}
        if self.fail_auth:
            await socket.send_json({"type": "auth_invalid"})
            return socket
        await socket.send_json({"type": "auth_ok"})
        subscription_id: int | None = None
        async for raw in socket:
            command = json.loads(raw.data)
            command_id = command["id"]
            if command["type"] == "subscribe_events":
                assert command["event_type"] == "state_changed"
                subscription_id = command_id
                await socket.send_json(
                    {"id": command_id, "type": "result", "success": True, "result": None}
                )
            elif command["type"] == "get_states":
                await socket.send_json(
                    {"id": command_id, "type": "result", "success": True, "result": [self.state]}
                )
            elif command["type"] == "call_service":
                self.commands.append(command)
                assert command["domain"] == "light"
                assert command["target"] == {"entity_id": ENTITY}
                if command["service"] == "turn_off":
                    self.state = {
                        "entity_id": ENTITY,
                        "state": "off",
                        "attributes": {"supported_color_modes": ["brightness"]},
                    }
                else:
                    brightness = 10 if self.report_stale else command["service_data"]["brightness"]
                    self.state = {
                        "entity_id": ENTITY,
                        "state": "on",
                        "attributes": {
                            "supported_color_modes": ["brightness"],
                            "brightness": brightness,
                        },
                    }
                assert subscription_id is not None
                await socket.send_json(
                    {
                        "id": subscription_id,
                        "type": "event",
                        "event": {
                            "event_type": "state_changed",
                            "data": {
                                "entity_id": ENTITY,
                                "new_state": self.state,
                            },
                        },
                    }
                )
                if self.lose_service:
                    await socket.close()
                    break
                await socket.send_json(
                    {
                        "id": command_id,
                        "type": "result",
                        "success": True,
                        "result": {"context": {"id": "fixture-context"}},
                    }
                )
        return socket


@asynccontextmanager
async def server(fake: FakeHomeAssistant) -> AsyncIterator[str]:
    app = web.Application()
    app.router.add_get("/api/websocket", fake.websocket)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    assert site._server is not None
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield f"ws://127.0.0.1:{port}/api/websocket"
    finally:
        await runner.cleanup()


def test_one_connection_reports_absolute_brightness_without_replaying() -> None:
    async def scenario() -> None:
        fake = FakeHomeAssistant()
        async with server(fake) as url:
            light = HomeAssistantLight(url, "test-token", ENTITY)
            await light.connect()
            first = await light.set_brightness(0.25)
            second = await light.set_brightness(0.50)
            assert first.available and abs(first.level - 0.25) <= 1 / 255
            assert second.available and abs(second.level - 0.50) <= 1 / 255
            assert light.generation == 1
            assert fake.connections == 1
            assert [command["service"] for command in fake.commands] == ["turn_on", "turn_on"]
            assert [command["service_data"]["brightness"] for command in fake.commands] == [64, 128]
            await light.close()

    asyncio.run(scenario())


def test_lost_service_result_is_not_replayed_on_explicit_reconnect() -> None:
    async def scenario() -> None:
        fake = FakeHomeAssistant(lose_service=True)
        async with server(fake) as url:
            light = HomeAssistantLight(url, "test-token", ENTITY, command_timeout_s=0.5)
            await light.connect()
            with pytest.raises(HomeAssistantLightError):
                await light.set_brightness(0.25)
            assert len(fake.commands) == 1
            with pytest.raises(HomeAssistantLightError, match="explicit reconciliation"):
                await light.set_brightness(0.50)
            assert len(fake.commands) == 1
            await light.reconnect()
            assert light.generation == 2
            assert len(fake.commands) == 1
            assert (await light.current_state()).level == 64 / 255
            await light.close()

    asyncio.run(scenario())


def test_unsupported_brightness_and_auth_failure_fail_closed() -> None:
    async def scenario() -> None:
        fake = FakeHomeAssistant()
        fake.state["attributes"] = {"supported_color_modes": ["onoff"]}
        async with server(fake) as url:
            light = HomeAssistantLight(url, "test-token", ENTITY)
            with pytest.raises(HomeAssistantLightError, match="does not support brightness"):
                await light.connect()
            assert fake.commands == []
            await light.close()
        denied = FakeHomeAssistant(fail_auth=True)
        async with server(denied) as url:
            light = HomeAssistantLight(url, "test-token", ENTITY)
            with pytest.raises(HomeAssistantLightError, match="authentication failed") as error:
                await light.connect()
            assert "test-token" not in str(error.value)
            await light.close()

    asyncio.run(scenario())


def test_non_loopback_plaintext_and_unbounded_levels_are_rejected() -> None:
    with pytest.raises(ValueError, match="require wss"):
        HomeAssistantLight("ws://homeassistant:8123/api/websocket", "token", ENTITY)
    light = HomeAssistantLight("ws://localhost:8123/api/websocket", "token", ENTITY)

    async def scenario() -> None:
        with pytest.raises(ValueError, match="absolute level"):
            await light.set_brightness(0.8)
        await light.close()

    asyncio.run(scenario())


def test_service_ack_without_matching_reported_state_is_uncertain() -> None:
    async def scenario() -> None:
        fake = FakeHomeAssistant(report_stale=True)
        async with server(fake) as url:
            light = HomeAssistantLight(url, "test-token", ENTITY, state_timeout_s=0.2)
            await light.connect()
            with pytest.raises(HomeAssistantLightError, match="not confirmed"):
                await light.set_brightness(0.25)
            assert len(fake.commands) == 1
            await light.close()

    asyncio.run(scenario())


class _Camera:
    def __init__(self) -> None:
        self.sequence = 0

    async def capture(self) -> CameraFrame:
        self.sequence += 1
        return CameraFrame(
            "panel-camera",
            "panel-lineage",
            f"frame-{self.sequence}",
            self.sequence,
            time.monotonic(),
            4,
            4,
            bytes(4 * 4 * 3),
        )


class _Worker:
    worker_id = "fixture-worker"
    program_id = "red-marker-v1"

    async def detect(self, frame: CameraFrame) -> MarkerMeasurement:
        return MarkerMeasurement(
            frame.source_id,
            frame.sample_id,
            self.worker_id,
            self.program_id,
            1.0,
            2.0,
            0.1 if frame.sequence <= 2 else 0.9,
        )


def test_ha_light_runs_through_inspection_operation_without_direct_tools() -> None:
    async def scenario() -> None:
        fake = FakeHomeAssistant()
        async with server(fake) as url:
            light = HomeAssistantLight(url, "test-token", ENTITY)
            host = OperationHost(
                (inspection_spec(_Camera(), _Worker(), light=light),),
                runtime_id="ha-protocol-fixture",
            )
            session = Session(host, owns_runtime=True)
            operation = await session.act(
                "inspect_target", {"target_id": "bench-marker"}, request_id="one"
            )
            result = await session.wait(operation, 5)
            assert result.lifecycle == Lifecycle.SUCCEEDED
            assert result.effect_state == EffectState.REPORTED
            assert result.result["sample_ids"] == ("frame-3", "frame-4")
            assert result.result["lighting_writes"] == 1
            assert len(fake.commands) == 1
            repeated = await session.act(
                "inspect_target", {"target_id": "bench-marker"}, request_id="one"
            )
            assert repeated.operation_id == operation.operation_id
            assert len(fake.commands) == 1
            await session.close()
            await light.close()

    asyncio.run(scenario())


def test_lost_ha_result_inhibits_task_level_effects() -> None:
    async def scenario() -> None:
        fake = FakeHomeAssistant(lose_service=True)
        async with server(fake) as url:
            light = HomeAssistantLight(url, "test-token", ENTITY, command_timeout_s=0.5)
            host = OperationHost(
                (inspection_spec(_Camera(), _Worker(), light=light),),
                runtime_id="ha-protocol-fixture",
            )
            session = Session(host, owns_runtime=True)
            operation = await session.act("inspect_target", {"target_id": "bench-marker"})
            result = await session.wait(operation, 5)
            assert result.lifecycle == Lifecycle.INDETERMINATE
            assert result.reason_code == "LIGHT_COMMAND_UNCONFIRMED"
            assert result.effect_state == EffectState.UNKNOWN
            assert (await session.observe()).effect_inhibited_reason == "LIGHT_COMMAND_UNCONFIRMED"
            assert len(fake.commands) == 1
            await session.close()
            await light.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["service_reply", "state_readback"])
@pytest.mark.parametrize("termination", ["cancel", "deadline"])
def test_interrupted_light_write_requires_reconciliation(
    monkeypatch: pytest.MonkeyPatch, phase: str, termination: str
) -> None:
    async def scenario() -> None:
        fake = FakeHomeAssistant(report_stale=phase == "state_readback")
        async with server(fake) as url:
            light = HomeAssistantLight(url, "test-token", ENTITY)
            reached = asyncio.Event()
            timeout = asyncio.timeout(None)
            original_command = light._command
            original_readback = light._wait_for_level

            async def held_command(command):
                result = await original_command(command)
                if command.get("type") == "call_service":
                    reached.set()
                    await asyncio.Event().wait()
                return result

            async def held_readback(level):
                state = await light._refresh_state()
                assert state.level == 10 / 255
                reached.set()
                await asyncio.Event().wait()

            async def write():
                async with timeout:
                    await light.set_brightness(0.25)

            monkeypatch.setattr(
                light,
                "_command" if phase == "service_reply" else "_wait_for_level",
                held_command if phase == "service_reply" else held_readback,
            )
            task = asyncio.create_task(write())
            try:
                await asyncio.wait_for(reached.wait(), 2)
                assert len(fake.commands) == 1
                if termination == "cancel":
                    task.cancel()
                else:
                    timeout.reschedule(asyncio.get_running_loop().time())
                with pytest.raises(
                    asyncio.CancelledError if termination == "cancel" else TimeoutError
                ):
                    await task
                monkeypatch.setattr(light, "_command", original_command)
                monkeypatch.setattr(light, "_wait_for_level", original_readback)
                fake.report_stale = False
                with pytest.raises(HomeAssistantLightError, match="explicit reconciliation"):
                    await light.set_brightness(0.5)
                assert len(fake.commands) == 1
                await light.reconnect()
                assert light.generation == 2
                assert len(fake.commands) == 1
                assert (await light.current_state()).available
                await light.set_brightness(0.5)
                assert [item["service_data"]["brightness"] for item in fake.commands] == [64, 128]
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await light.close()

    asyncio.run(scenario())


def test_cancel_before_dispatch_does_not_inhibit_light(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        fake = FakeHomeAssistant()
        async with server(fake) as url:
            light = HomeAssistantLight(url, "test-token", ENTITY)
            await light.connect()
            reached = asyncio.Event()
            original = light._refresh_state

            async def held_state():
                reached.set()
                await asyncio.Event().wait()

            monkeypatch.setattr(light, "_refresh_state", held_state)
            task = asyncio.create_task(light.set_brightness(0.25))
            try:
                await asyncio.wait_for(reached.wait(), 2)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert fake.commands == []
                monkeypatch.setattr(light, "_refresh_state", original)
                await light.set_brightness(0.5)
                assert len(fake.commands) == 1
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await light.close()

    asyncio.run(scenario())
