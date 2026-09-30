import asyncio

import pytest

from entryplug_app.contracts import AppError, validate_definition
from entryplug_app.service import ApplicationService
from entryplug_app.workspace import Workspace


def definition(**changes):
    return {"name": "Watch entrance", "instructions": "Alert on validated entry", **changes}


async def settle(service, run_id, count=1):
    for _ in range(200):
        run = await service.store.get("runs", run_id)
        if run["turn_count"] >= count and not run["active_turn_id"]:
            return run
        await asyncio.sleep(0.01)
    raise AssertionError(run)


@pytest.mark.asyncio
async def test_watch_lifetime_alert_occurrences_and_recovery(tmp_path):
    workspace = Workspace.resolve(str(tmp_path))
    service = ApplicationService(workspace)
    await service.open()
    try:
        mission = await service.command("mission.create", definition(), "create")
        run = await service.command("mission.start", {"mission_id": mission["id"]}, "start")
        duplicate = await service.command(
            "mission.start", {"mission_id": mission["id"]}, "another-client"
        )
        assert duplicate["id"] == run["id"]
        assert (await service.store.get("runs", run["id"]))["model_calls"] == 0
        for action, count in [
            ("enter_a", 1),
            ("presence_a", 1),
            ("enter_b", 2),
            ("exit_a", 2),
            ("enter_a", 3),
        ]:
            await service.demo_step(action)
            await settle(service, run["id"], count)
        alerts = await service.store.list("alerts")
        assert len(alerts) == 3
        assert all(a["evidence_ids"] for a in alerts)
        await service.demo_step("lost")
        assert (await service.store.get("runs", run["id"]))["health"] == "blocked"
        await service.demo_step("recover")
        await service.demo_step("enter_a")
        final = await settle(service, run["id"], 4)
        assert final["lifecycle"] == "active" and final["activity"] == "waiting_event"
        assert final["model_calls"] == 0
        await service.demo_step("stale")
        assert (await service.store.get("runs", run["id"]))["reason_code"] == "stale_observation"
        snapshot = await service.snapshot()
    finally:
        await service.close()
    recovered = ApplicationService(workspace)
    await recovered.open()
    try:
        assert len(await recovered.store.list("alerts")) == 4
        assert (await recovered.store.get("runs", run["id"]))[
            "reason_code"
        ] == "revalidation_required"
        assert (await recovered.snapshot())["cursor"] > snapshot["cursor"]
        stopped = await recovered.command("run.stop", {"run_id": run["id"]}, "stop")
        assert stopped["lifecycle"] == "stopped"
    finally:
        await recovered.close()


@pytest.mark.asyncio
async def test_owner_dedup_revision_and_scope(tmp_path):
    service = ApplicationService(Workspace.resolve(str(tmp_path)))
    await service.open()
    try:
        other = ApplicationService(service.workspace)
        with pytest.raises(AppError, match="already owns"):
            await other.open()
        mission = await service.command("mission.create", definition(), "key")
        assert await service.command("mission.create", definition(), "key") == mission
        with pytest.raises(AppError, match="different content"):
            await service.command("mission.create", definition(name="Changed"), "key")
        with pytest.raises(AppError, match="If-Match"):
            await service.command(
                "mission.update",
                {"mission_id": mission["id"], "definition": definition()},
                "edit",
                revision=0,
            )
        run = await service.command("mission.start", {"mission_id": mission["id"]}, "start")
        op = await service.command(
            "operation.create",
            {"run_id": run["id"], "capability": "camera.snapshot", "arguments": {}},
            "op",
        )
        for _ in range(100):
            result = await service.store.get("operations", op["id"])
            if result["lifecycle"] == "succeeded":
                break
            await asyncio.sleep(0.01)
        assert result["lifecycle"] == "succeeded"
        assert result["native_operation_id"] and result["evidence_ids"]
        await service.command("run.pause", {"run_id": run["id"]}, "pause")
        with pytest.raises(AppError, match="admissions are closed"):
            await service.command(
                "operation.create",
                {"run_id": run["id"], "capability": "camera.snapshot"},
                "paused-op",
            )
    finally:
        await service.close()


def test_shared_contract_rejects_conflicting_rules_and_unknown_fields():
    for change in (
        {"agent": {"profile": "paid"}},
        {"limits": {"max_concurrent_turns": 2}},
        {"instructions_file": "/etc/passwd"},
        {"trigger": {"event": "motion.started"}},
    ):
        with pytest.raises(AppError):
            validate_definition(definition(**change))
