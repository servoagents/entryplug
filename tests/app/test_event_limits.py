import asyncio

import pytest

from entryplug_app.service import ApplicationService
from entryplug_app.workspace import Workspace


@pytest.mark.asyncio
async def test_slow_observer_does_not_block_other_observers_or_alerts(tmp_path):
    service = ApplicationService(Workspace.resolve(str(tmp_path)))
    await service.open()
    slow, fast = asyncio.Queue(maxsize=2), asyncio.Queue(maxsize=2)
    service.subscribers.update((slow, fast))
    try:
        for index in range(1000):
            service.transient({"type": "turn.text", "delta": str(index)})
            assert (await fast.get())["delta"] == str(index)
        assert slow.qsize() <= 2
        assert (await slow.get())["type"] == "resync"
        mission = await service.command(
            "mission.create",
            {"name": "Observe", "instructions": "Alert on labelled entry"},
            "create",
        )
        await service.command("mission.start", {"mission_id": mission["id"]}, "start")
        await service.demo_step("enter_a")
        async with asyncio.timeout(2):
            while not await service.store.list("alerts"):
                await asyncio.sleep(0.01)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_concurrent_snapshot_cursor_and_retention_preserve_evidence(tmp_path):
    service = ApplicationService(Workspace.resolve(str(tmp_path)), event_retention=2)
    await service.open()
    try:
        for index in range(20):
            identifier = f"evidence-{index}"
            commit, snapshot = await asyncio.gather(
                service.store.commit(
                    [("alerts", identifier, {"id": identifier})],
                    "alert.created",
                    {"id": identifier},
                ),
                service.store.snapshot(),
            )
            assert (identifier in {a["id"] for a in snapshot["alerts"]}) == (
                snapshot["cursor"] >= commit["seq"]
            )
        assert len(await service.store.list("alerts")) == 20
        assert len(await service.store.events(commit["seq"] - 2)) == 2
    finally:
        await service.close()
