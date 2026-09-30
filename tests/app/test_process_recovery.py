"""Abrupt OS process loss around a real OperationHost dispatch (no hardware)."""

import asyncio
import os
import subprocess
import sys

import pytest

from entryplug_app.service import ApplicationService
from entryplug_app.workspace import Workspace

CHILD = r"""
import asyncio, os, sys
from pathlib import Path
from entryplug.core.operation import CapabilitySpec, OperationHost
from entryplug.harness.session import Session
from entryplug_app.ports import EmbodimentPort
from entryplug_app.service import ApplicationService
from entryplug_app.workspace import Workspace

root, stage = Path(sys.argv[1]), sys.argv[2]
async def effect(context, arguments):
    with (root / "effect-count").open("a") as stream:
        stream.write("effect\n"); stream.flush(); os.fsync(stream.fileno())
    os._exit(73)

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
               Transport(OperationHost([spec]), owns_runtime=True))])
    await service.open()
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
def test_process_death_preserves_intent_and_never_replays(tmp_path, stage):
    child = subprocess.run(
        [sys.executable, "-c", CHILD, str(tmp_path), stage],
        env={**os.environ, "PYTHONPATH": "src"},
        timeout=10,
        capture_output=True,
    )
    assert child.returncode == 73, child.stderr.decode()

    async def recover():
        service = ApplicationService(Workspace.resolve(str(tmp_path)), demo=False)
        await service.open()
        try:
            operations = await service.store.list("operations")
            assert len(operations) == 1
            assert operations[0]["lifecycle"] == (
                "failed" if stage == "before" else "indeterminate"
            )
            await asyncio.sleep(0.02)
        finally:
            await service.close()

    asyncio.run(recover())
    marker = tmp_path / "effect-count"
    assert marker.read_text() == "effect\n" if stage == "after_effect" else not marker.exists()
