"""An interrupted ready handshake must not orphan its already-started world."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import entryplug_harbor.resident as resident
from entryplug_harbor.episode import StopReport


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmed", [True, False])
async def test_cancel_during_ready_stops_owned_world(monkeypatch, tmp_path, confirmed):
    initializing = asyncio.Event()
    runs = []

    class StartingRun:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.evidence_path = kwargs["evidence_path"]
            self.stop_calls = 0
            runs.append(self)

        async def initialize(self):
            initializing.set()
            await asyncio.Event().wait()

        async def stop(self):
            self.stop_calls += 1
            self.kwargs["temporary_log"].close()
            self.kwargs["temporary_log_path"].unlink(missing_ok=True)
            return StopReport(confirmed, False)

    async def spawn(*args, **kwargs):
        return SimpleNamespace(returncode=None)

    monkeypatch.setattr(resident, "DockerResidentRun", StartingRun)
    monkeypatch.setattr(resident.asyncio, "create_subprocess_exec", spawn)
    task = asyncio.create_task(resident.start_owned_resident(tmp_path, "fixture", "cancel-start"))
    try:
        await asyncio.wait_for(initializing.wait(), 1)
        task.cancel()
        with pytest.raises(
            asyncio.CancelledError if confirmed else resident.ResidentStartupFailure
        ):
            await task
        assert runs[0].stop_calls == 1
    finally:
        for run in runs:
            run.kwargs["temporary_log"].close()
            run.kwargs["temporary_log_path"].unlink(missing_ok=True)
