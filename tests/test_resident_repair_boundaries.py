"""Repair-phase client failures must never create a second native task."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

import entryplug.harbor_resident as resident
from entryplug.harbor_episode import StopReport
from entryplug.operation import AdmissionError, Lifecycle, MotionState
from entryplug.recovery_qualification import HARBOR_RESIDENT_RECOVERY_V1
from entryplug.visual_task import VISUAL_REACH


@dataclass
class RepairRun:
    run_id: str
    evidence_path: Path
    mode: str
    ready: dict[str, object] = field(
        default_factory=lambda: {
            "source_id": resident.SOURCE_ID,
            "lineage_id": resident.SOURCE_LINEAGE,
            "initial_y_px": 220.0,
        }
    )
    requests: list[str] = field(default_factory=list)
    cancellations: list[str] = field(default_factory=list)
    stop_calls: int = 0
    repair_started: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    async def request(
        self,
        operation_id: str,
        target_y_px: float,
        *,
        evaluation_fault: str | None = None,
        evaluation_recovery_strategy: str = "checked_reuse",
        deadline_monotonic: float | None = None,
    ) -> dict[str, object]:
        self.requests.append(operation_id)
        if evaluation_fault is not None:
            assert evaluation_fault == "kill-active-worker"
            assert evaluation_recovery_strategy == "checked_reuse"
            assert isinstance(deadline_monotonic, float)
            self.repair_started.set()
            await self.release.wait()
            if self.mode == "lost":
                raise resident.ResidentReplyLost("reply lost during repair")
        return {
            "version": 1,
            "type": "result",
            "operation_id": operation_id,
            "status": "passed",
            "source_id": resident.SOURCE_ID,
            "lineage_id": resident.SOURCE_LINEAGE,
            "target_y_px": target_y_px,
            "quiescence_confirmed": True,
            "visual_capability_available": True,
        }

    async def cancel(self, operation_id: str) -> None:
        self.cancellations.append(operation_id)
        if self.mode != "hung":
            self.release.set()

    async def stop(self) -> StopReport:
        self.stop_calls += 1
        self.release.set()
        return StopReport(True, False)


async def _session(tmp_path: Path, run: RepairRun) -> resident.ResidentSession:
    factory = resident.harbor_resident_episode_factory(
        tmp_path,
        start_resident=lambda *_args: asyncio.sleep(0, result=run),
        image_check=lambda _image: asyncio.sleep(0, result=True),
        evaluation_fault="kill-active-worker",
    )
    episode = await factory(101)
    first = await episode.session.act(VISUAL_REACH, {"target_y_px": 224}, request_id="first")
    assert (await episode.session.wait(first, 1.0)).lifecycle == Lifecycle.SUCCEEDED
    assert isinstance(episode.session, resident.ResidentSession)
    return episode.session


def test_cancel_during_repair_reconciles_late_pass_without_replaying(tmp_path: Path) -> None:
    run = RepairRun("repair-cancel", tmp_path / "runs" / "repair-cancel", "late-pass")

    async def scenario() -> None:
        session = await _session(tmp_path, run)
        second = await session.act(VISUAL_REACH, {"target_y_px": 216}, request_id="second")
        await asyncio.wait_for(run.repair_started.wait(), 1.0)
        repeated = await session.act(VISUAL_REACH, {"target_y_px": 216}, request_id="second")
        assert repeated.operation_id == second.operation_id
        assert len(run.requests) == 2
        await session.cancel(second)
        result = await session.wait(second, 1.0)
        assert result.lifecycle == Lifecycle.CANCELED
        assert result.motion_state == MotionState.HOLDING
        assert run.cancellations == [second.operation_id]
        assert len(run.requests) == 2
        await session.close()
        assert run.stop_calls == 1

    asyncio.run(scenario())


def test_lost_reply_during_repair_stops_world_and_fences_admission(tmp_path: Path) -> None:
    run = RepairRun("repair-lost", tmp_path / "runs" / "repair-lost", "lost")

    async def scenario() -> None:
        session = await _session(tmp_path, run)
        second = await session.act(VISUAL_REACH, {"target_y_px": 216}, request_id="second")
        await asyncio.wait_for(run.repair_started.wait(), 1.0)
        run.release.set()
        result = await session.wait(second, 1.0)
        assert result.lifecycle == Lifecycle.FAILED
        assert result.reason_code == "REPLY_LOST_STOPPED"
        assert result.motion_state == MotionState.IDLE
        assert run.stop_calls == 1
        repeated = await session.act(VISUAL_REACH, {"target_y_px": 216}, request_id="second")
        assert repeated.operation_id == second.operation_id
        assert len(run.requests) == 2
        with pytest.raises(AdmissionError, match="resident world is unavailable"):
            await session.act(VISUAL_REACH, {"target_y_px": 218}, request_id="third")
        await session.close()

    asyncio.run(scenario())


def test_deadline_during_hung_repair_inhibits_motion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = RepairRun("repair-deadline", tmp_path / "runs" / "repair-deadline", "hung")
    short_profile = replace(
        HARBOR_RESIDENT_RECOVERY_V1,
        operation_deadline_seconds=0.5,
        cancellation_grace_seconds=0.1,
    )
    monkeypatch.setattr(resident, "HARBOR_RESIDENT_RECOVERY_V1", short_profile)

    async def scenario() -> None:
        session = await _session(tmp_path, run)
        second = await session.act(VISUAL_REACH, {"target_y_px": 216}, request_id="second")
        await asyncio.wait_for(run.repair_started.wait(), 1.0)
        result = await session.wait(second, 2.0)
        assert result.lifecycle == Lifecycle.INDETERMINATE
        assert result.motion_state == MotionState.UNKNOWN
        assert result.reason_code == "STOP_UNCONFIRMED"
        assert run.cancellations == [second.operation_id]
        assert (await session.observe()).motion_inhibited_reason == "STOP_UNCONFIRMED"
        with pytest.raises(AdmissionError, match="motion is inhibited"):
            await session.act(VISUAL_REACH, {"target_y_px": 218}, request_id="third")
        assert len(run.requests) == 2
        await session.close()
        assert run.stop_calls == 1

    asyncio.run(scenario())
