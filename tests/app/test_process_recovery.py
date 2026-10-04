"""Abrupt OS process loss around a real OperationHost dispatch (no hardware)."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from entryplug.core.operation import (
    CapabilitySpec,
    Lifecycle,
    MotionState,
    OperationHost,
    OperationResult,
)
from entryplug.harness.session import Session
from entryplug_app.contracts import AppError, utc_now
from entryplug_app.ports import EmbodimentPort
from entryplug_app.service import ApplicationService
from entryplug_app.workspace import Workspace

CHILD = r"""
import asyncio, os, sys
from pathlib import Path
from entryplug.core.operation import (CapabilitySpec, OperationHost,
                                      Lifecycle, MotionState, OperationResult)
from entryplug.harness.session import Session
from entryplug_app.ports import EmbodimentPort
from entryplug_app.service import ApplicationService
from entryplug_app.workspace import Workspace

root, stage = Path(sys.argv[1]), sys.argv[2]
async def effect(context, arguments):
    with (root / "effect-count").open("a") as stream:
        stream.write("effect\n"); stream.flush(); os.fsync(stream.fileno())
    os._exit(73)

async def read(context, arguments):
    return OperationResult(Lifecycle.SUCCEEDED, MotionState.IDLE)

class Transport(Session):
    async def act(self, *args, **kwargs):
        if stage == "sending":
            os._exit(73)
        return await super().act(*args, **kwargs)

async def main():
    spec = CapabilitySpec("fixture.effect", "1", "Explicit effect fixture", False,
                         10, .1, lambda a: a, effect, {"type":"object"}, physical_effects=True)
    service = ApplicationService(Workspace.resolve(str(root)), demo=False,
        ports=[EmbodimentPort("fixture", "Fixture",
               Transport(OperationHost([spec, CapabilitySpec(
                   "fixture.read", "1", "Read only", False,
                   10, .1, lambda a: a, read, {"type": "object"})]), owns_runtime=True))])
    await service.open()
    for name, allow in [("writer", ["fixture.effect"]), ("reader", ["fixture.read"])]:
        mission = await service.command("mission.create", {
            "name": name, "instructions": "Wait for an event",
            "body": {"selector": "fixture", "required_capabilities": []},
            "access": {"allow": allow},
            "trigger": {"source": "fixture"},
        }, name)
        await service.command("mission.start", {"mission_id": mission["id"]}, name + "-start")
    attachment = await service.command("attachment.create", {
        "name":"Test", "body_id":"fixture", "allow":["fixture.effect"]}, "attach")
    if stage == "before":
        async def stop_before_dispatch(identifier):
            os._exit(73)
        service._dispatch = stop_before_dispatch
    await service.command("operation.create", {"attachment_id":attachment["id"],
                          "capability":"fixture.effect"}, "physical-command")
    await asyncio.sleep(30)
asyncio.run(main())
"""


@pytest.mark.parametrize("stage", ["before", "sending", "after_effect"])
@pytest.mark.parametrize("admission", ["resume", "start", "revalidate"])
def test_process_death_preserves_intent_and_never_replays(tmp_path, stage, admission):
    child = subprocess.run(
        [sys.executable, "-c", CHILD, str(tmp_path), stage],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")},
        timeout=10,
        capture_output=True,
    )
    assert child.returncode == 73, child.stderr.decode()

    async def recover():
        async def effect(context, arguments):
            with (tmp_path / "effect-count").open("a") as stream:
                stream.write("replayed\n")
            return OperationResult(Lifecycle.SUCCEEDED, MotionState.IDLE)

        spec = CapabilitySpec(
            "fixture.effect",
            "1",
            "Physical fixture",
            False,
            10,
            0.1,
            lambda a: a,
            effect,
            {"type": "object"},
            physical_effects=True,
        )

        async def read(context, arguments):
            return OperationResult(Lifecycle.SUCCEEDED, MotionState.IDLE)

        readonly = CapabilitySpec(
            "fixture.read",
            "1",
            "Read only",
            False,
            10,
            0.1,
            lambda a: a,
            read,
            {"type": "object"},
        )
        session = Session(OperationHost([spec, readonly]), owns_runtime=True)
        service = ApplicationService(
            Workspace.resolve(str(tmp_path)),
            demo=False,
            ports=[EmbodimentPort("fixture", "Fixture", session)],
        )
        await service.open()
        try:
            operations = await service.store.list("operations")
            assert len(operations) == 1
            assert operations[0]["lifecycle"] == (
                "failed" if stage == "before" else "indeterminate"
            )
            op = operations[0]
            assert op["reason_code"] == (
                "NOT_DISPATCHED" if stage == "before" else "RESTART_UNCONFIRMED"
            )
            assert op["effect_state"] == ("none" if stage == "before" else "unknown")
            # A new host has no memory of the old command: the durable owner must fence it.
            view = await session.observe()
            assert view.runtime_id != op["session_runtime_id"]
            assert not view.operations and view.effect_inhibited_reason is None
            duplicate = await service.command(
                "operation.create",
                {
                    "attachment_id": op["attachment_id"],
                    "capability": "fixture.effect",
                },
                "physical-command",
            )
            assert duplicate["id"] == op["id"]
            runs = {r["name"]: r for r in await service.store.list("runs")}
            if admission == "revalidate":
                await service.ingest(
                    {
                        "source": "fixture",
                        "type": "source.revalidated",
                        "epoch": "new",
                        "cursor": 1,
                        "at": utc_now(),
                        "occurrence": "reconnected",
                    }
                )
            else:
                for name, run in runs.items():
                    if admission == "start":
                        await service.command("run.stop", {"run_id": run["id"]}, name + "-stop")
                        action, payload = "mission.start", {"mission_id": run["mission_id"]}
                    else:
                        action, payload = "run.resume", {"run_id": run["id"]}
                    if name == "writer" and stage != "before":
                        with pytest.raises(AppError) as refused:
                            await service.command(action, payload, name + "-recover")
                        assert refused.value.code == "resource_indeterminate"
                    else:
                        runs[name] = await service.command(action, payload, name + "-recover")
            writer = await service.store.get("runs", runs["writer"]["id"])
            reader = await service.store.get("runs", runs["reader"]["id"])
            assert writer["health"] == ("ok" if stage == "before" else "blocked")
            assert reader["health"] == "ok"
            if admission == "revalidate" and stage != "before":
                assert writer["reason_code"] == "resource_indeterminate"
            if stage != "before":
                with pytest.raises(AppError) as refused:
                    await service.command(
                        "operation.create",
                        {
                            "attachment_id": op["attachment_id"],
                            "capability": "fixture.effect",
                        },
                        "fresh-command",
                    )
                assert refused.value.code == "resource_indeterminate"
            read_op = await service.command(
                "operation.create",
                {
                    "run_id": reader["id"],
                    "capability": "fixture.read",
                },
                "read-after-restart",
            )
            async with asyncio.timeout(2):
                while (await service.store.get("operations", read_op["id"]))[
                    "lifecycle"
                ] != "succeeded":
                    await asyncio.sleep(0.01)
            assert len((await session.observe()).operations) == 1
        finally:
            await service.close()

    asyncio.run(recover())
    marker = tmp_path / "effect-count"
    assert marker.read_text() == "effect\n" if stage == "after_effect" else not marker.exists()
