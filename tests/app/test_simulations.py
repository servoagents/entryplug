import asyncio

import pytest

from entryplug_app.contracts import AppError
from entryplug_app.service import ApplicationService
from entryplug_app.workspace import Workspace


async def wait_turn(service, run_id):
    async with asyncio.timeout(5):
        while True:
            run = await service.store.get("runs", run_id)
            if run["turn_count"] and not run["active_turn_id"]:
                return run
            await asyncio.sleep(0.02)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,identifier,action,read,write",
    [
        ("rover", "demo.rover", "tick", "robot.observe", "robot.move"),
        ("room", "demo.room", "arrive", "room.observe", "room.set_light"),
    ],
)
async def test_simulated_agent_changes_its_body_through_journalled_tools(
    tmp_path, kind, identifier, action, read, write
):
    service = ApplicationService(Workspace.resolve(str(tmp_path)))
    await service.open()
    try:
        mission = await service.command(
            "mission.create",
            {
                "name": "Interactive simulation",
                "instructions": "Run the demonstration policy",
                "agent": {"driver": "simulated", "decision_mode": "assisted"},
                "body": {"selector": identifier, "required_capabilities": [read]},
                "trigger": {"source": identifier, "event": "simulation.changed"},
                "access": {"allow": [read, write, "alerts.emit"]},
            },
            "create",
        )
        run = await service.command("mission.start", {"mission_id": mission["id"]}, "start")
        await service.simulations.control(identifier, {"action": "inject", "event": action})
        finished = await wait_turn(service, run["id"])
        assert finished["model_calls"] == 0
        state = service.simulations.describe(identifier)["state"]
        assert state["waypoint"] == "aisle" if kind == "rover" else state["light"] is True
        operations = await service.store.list("operations")
        assert {op["capability"] for op in operations} == {read, write}
        assert all(op["lifecycle"] == "succeeded" and op["evidence_ids"] for op in operations)
        assert len(await service.store.list("alerts")) == 1
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_playback_is_owned_by_service_and_custom_bodies_restore_paused(tmp_path):
    workspace = Workspace.resolve(str(tmp_path))
    service = ApplicationService(workspace)
    await service.open()
    body = await service.simulations.create({"kind": "camera", "name": "Side entrance"})
    identifier = body["id"]
    await service.simulations.control(identifier, {"action": "play"})
    # No browser, SSE subscriber or client clock is involved.
    await asyncio.sleep(0.15)
    playing = service.simulations.describe(identifier)
    assert playing["playing"] and playing["position_s"] > 0.1
    await service.simulations.control(identifier, {"action": "pause"})
    paused = service.simulations.describe(identifier)["position_s"]
    await asyncio.sleep(0.1)
    assert service.simulations.describe(identifier)["position_s"] == paused
    await service.close()
    recovered = ApplicationService(workspace)
    await recovered.open()
    try:
        restored = recovered.simulations.describe(identifier)
        assert restored["name"] == "Side entrance" and not restored["playing"]
        assert restored["position_s"] == 0
    finally:
        await recovered.close()


@pytest.mark.asyncio
async def test_simulated_agent_rejects_real_body_and_disabled_simulation_creation(tmp_path):
    service = ApplicationService(Workspace.resolve(str(tmp_path)))
    await service.open()
    try:
        service.ports["demo.access-camera"].simulated = False
        with pytest.raises(AppError, match="simulated body"):
            await service.validate(
                {
                    "name": "No hardware",
                    "instructions": "Demo",
                    "agent": {"driver": "simulated", "decision_mode": "assisted"},
                }
            )
    finally:
        await service.close()
    disabled = ApplicationService(Workspace.resolve(str(tmp_path / "disabled")), demo=False)
    await disabled.open()
    try:
        with pytest.raises(AppError, match="disabled"):
            await disabled.simulations.create({"kind": "rover", "name": "Unavailable"})
        assert disabled.simulations.snapshot() == []
    finally:
        await disabled.close()
