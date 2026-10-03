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


@pytest.mark.asyncio
@pytest.mark.parametrize("wire", [b"native diagnostic\n", b"\xff\n", b'{"version":2}\n'])
async def test_invalid_private_record_retains_bounded_local_diagnostic(tmp_path, wire):
    from types import SimpleNamespace

    from entryplug_harbor.resident import DockerResidentRun, ResidentReplyLost

    reader = asyncio.StreamReader()
    reader.feed_data(wire)
    reader.feed_eof()
    log = tmp_path / "temporary.log"
    with log.open("wb") as stream:
        run = DockerResidentRun(
            run_id="bad-record",
            evidence_path=tmp_path / "evidence",
            container_name="owned-test",
            process=SimpleNamespace(stdin=object(), stdout=reader),
            temporary_log_path=log,
            temporary_log=stream,
        )
        with pytest.raises(ResidentReplyLost):
            await run._read(1)
        artifacts = list(run.evidence_path.glob("invalid-record-*.bin"))
        assert len(artifacts) == 1
        assert artifacts[0].read_bytes() == wire
        assert artifacts[0].stat().st_mode & 0o777 == 0o600
