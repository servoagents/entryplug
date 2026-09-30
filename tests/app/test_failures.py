import asyncio
import time

import pytest

from entryplug.core.operation import (
    CapabilitySpec,
    Lifecycle,
    MotionState,
    OperationHost,
    OperationResult,
)
from entryplug.harness.session import Session
from entryplug_app.contracts import AppError
from entryplug_app.ports import EmbodimentPort
from entryplug_app.service import ApplicationService
from entryplug_app.workspace import Workspace


def definition(**changes):
    return {"name": "Watch", "instructions": "Watch labelled events", **changes}


async def start(service, value=None):
    mission = await service.command("mission.create", value or definition(), "create")
    run = await service.command("mission.start", {"mission_id": mission["id"]}, "start")
    return mission, run


@pytest.mark.asyncio
async def test_pending_queue_is_bounded_and_source_loss_closes_turn(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    class Slow:
        async def turn(self, context):
            entered.set()
            await release.wait()
            await context.tools.call("alerts.emit", {"message": "late"})
            return "finished"

    service = ApplicationService(Workspace.resolve(str(tmp_path)), allow_scripted=True)
    service.drivers["scripted"] = Slow()
    await service.open()
    try:
        _, run = await start(
            service,
            definition(
                agent={"driver": "scripted", "decision_mode": "assisted"},
                trigger={"max_pending": 2},
            ),
        )
        await service.demo_step("enter_a")
        await entered.wait()
        for _ in range(5):
            await service.demo_step("exit_a")
            await service.demo_step("enter_a")
        current = await service.store.get("runs", run["id"])
        assert len(current["pending"]) == 2
        assert current["dropped_events"] == 3
        await service.demo_step("lost")
        release.set()
        await asyncio.sleep(0.02)
        assert not await service.store.list("alerts")
        assert (await service.store.get("runs", run["id"]))["health"] == "blocked"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_approval_binds_revision_and_exact_content(tmp_path):
    service = ApplicationService(Workspace.resolve(str(tmp_path)))
    await service.open()
    try:
        value = definition(
            access={
                "allow": ["person.events", "camera.snapshot", "alerts.emit"],
                "approve": ["camera.snapshot"],
            }
        )
        mission, run = await start(service, value)
        op = await service.command(
            "operation.create", {"run_id": run["id"], "capability": "camera.snapshot"}, "op"
        )
        assert op["dispatch"] == "waiting_approval"
        assert not (await service.demo.port.session.observe()).operations
        await service.command(
            "mission.update",
            {
                "mission_id": mission["id"],
                "definition": {**value, "instructions": "New instructions"},
            },
            "edit",
            revision=1,
        )
        with pytest.raises(AppError, match="changed"):
            await service.command(
                "approval.decision", {"approval_id": op["approval_id"], "allow": True}, "approve"
            )
        await service.command(
            "approval.decision", {"approval_id": op["approval_id"], "allow": False}, "deny"
        )
        assert (await service.store.get("operations", op["id"]))["lifecycle"] == "rejected"
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dispatch,expected",
    [("pending", "failed"), ("dispatching", "indeterminate"), ("sent", "indeterminate")],
)
async def test_crash_journal_never_replays_uncertain_physical_work(tmp_path, dispatch, expected):
    calls = []

    async def effect(context, arguments):
        calls.append(arguments)
        return OperationResult(Lifecycle.SUCCEEDED, MotionState.IDLE)

    spec = CapabilitySpec(
        "fixture.effect",
        "1",
        "Physical fixture effect",
        False,
        2,
        0.1,
        lambda a: a,
        effect,
        {"type": "object", "properties": {}},
        physical_effects=True,
    )
    workspace = Workspace.resolve(str(tmp_path))
    service = ApplicationService(
        workspace,
        ports=[
            EmbodimentPort("fixture", "Fixture", Session(OperationHost([spec]), owns_runtime=True))
        ],
    )
    await service.open()
    attachment = await service.command(
        "attachment.create",
        {"name": "Test", "body_id": "fixture", "allow": ["fixture.effect"]},
        "attach",
    )
    # Inject the persisted states at the three crash boundaries without dispatching.
    op = await service._operation_intent(
        {"attachment_id": attachment["id"], "capability": "fixture.effect"}, "owner"
    )
    op["dispatch"] = dispatch
    await service.store.commit([("operations", op["id"], op)], "operation.intent", op)
    # Mimic abrupt process death: do not run the orderly owner close path.
    await service.store.close()
    service.owner.release()
    await service.ports["fixture"].close()
    await service.demo.port.close()
    recovered = ApplicationService(
        workspace,
        ports=[
            EmbodimentPort("fixture", "Fixture", Session(OperationHost([spec]), owns_runtime=True))
        ],
    )
    await recovered.open()
    try:
        assert (await recovered.store.get("operations", op["id"]))["lifecycle"] == expected
        assert calls == []
        if expected == "indeterminate":
            with pytest.raises(AppError, match="Reconcile"):
                await recovered.command(
                    "operation.create",
                    {"attachment_id": attachment["id"], "capability": "fixture.effect"},
                    "new-key",
                )
    finally:
        await recovered.close()


@pytest.mark.asyncio
async def test_replayed_source_cursor_and_idle_do_not_wake_model(tmp_path, monkeypatch):
    service = ApplicationService(Workspace.resolve(str(tmp_path)), allow_scripted=True)
    await service.open()
    try:
        _, run = await start(
            service, definition(agent={"driver": "scripted", "decision_mode": "assisted"})
        )
        before = time.time()
        monkeypatch.setattr("entryplug_app.service.time.time", lambda: before + 600)
        await asyncio.sleep(0.01)
        assert (await service.store.get("runs", run["id"]))["model_calls"] == 0
        monkeypatch.undo()
        event = await service.demo_step("enter_a")
        for _ in range(100):
            if (await service.store.get("runs", run["id"]))["turn_count"]:
                break
            await asyncio.sleep(0.01)
        await service.ingest(event)
        await asyncio.sleep(0.01)
        assert (await service.store.get("runs", run["id"]))["model_calls"] == 1
        assert len(await service.store.list("alerts")) == 1
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_pausing_a_turn_invalidates_its_undispatched_approval(tmp_path):
    class WaitForApproval:
        async def turn(self, context):
            await context.tools.call("camera.snapshot", {})
            return "Read"

    service = ApplicationService(Workspace.resolve(str(tmp_path)), allow_scripted=True)
    service.drivers["scripted"] = WaitForApproval()
    await service.open()
    try:
        _, run = await start(
            service,
            definition(
                agent={"driver": "scripted", "decision_mode": "assisted"},
                trigger={"kind": "manual"},
                access={
                    "allow": ["camera.snapshot", "person.events"],
                    "approve": ["camera.snapshot"],
                },
            ),
        )
        async with asyncio.timeout(2):
            while not await service.store.list("approvals"):
                await asyncio.sleep(0.01)
        await service.command("run.pause", {"run_id": run["id"]}, "pause")
        async with asyncio.timeout(2):
            while (await service.store.list("approvals"))[0]["status"] == "pending":
                await asyncio.sleep(0.01)
        assert (await service.store.list("operations"))[0]["lifecycle"] == "canceled"
        assert not (await service.demo.port.session.observe()).operations
    finally:
        await service.close()
