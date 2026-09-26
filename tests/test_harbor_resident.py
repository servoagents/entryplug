from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from entryplug.harbor_episode import StopReport
from entryplug.harbor_resident import (
    SOURCE_ID,
    SOURCE_LINEAGE,
    harbor_resident_episode_factory,
)
from entryplug.operation import AdmissionError, Lifecycle
from entryplug.visual_task import VISUAL_REACH


@dataclass
class FakeResident:
    run_id: str
    evidence_path: Path
    ready: dict[str, object] = field(default_factory=lambda: {
        "source_id": SOURCE_ID,
        "lineage_id": SOURCE_LINEAGE,
        "initial_y_px": 220.0,
        "acquisition_ms": 3000.0,
    })
    requests: list[tuple[str, float]] = field(default_factory=list)
    canceled: list[str] = field(default_factory=list)
    stop_calls: int = 0
    pending: asyncio.Event | None = None
    quiescence_confirmed: bool = True

    async def request(self, operation_id: str, target_y_px: float) -> dict[str, object]:
        self.requests.append((operation_id, target_y_px))
        if self.pending is not None:
            await self.pending.wait()
        return {
            "version": 1,
            "type": "result",
            "operation_id": operation_id,
            "status": "canceled" if operation_id in self.canceled else "passed",
            "source_id": SOURCE_ID,
            "lineage_id": SOURCE_LINEAGE,
            "target_y_px": target_y_px,
            "final_y_px": target_y_px,
            "final_error_px": 0.0,
            "quiescence_confirmed": self.quiescence_confirmed,
        }

    async def cancel(self, operation_id: str) -> None:
        self.canceled.append(operation_id)
        if self.pending is not None:
            self.pending.set()

    async def stop(self) -> StopReport:
        self.stop_calls += 1
        return StopReport(True, False)


def test_two_tasks_keep_one_world_and_one_runtime(tmp_path: Path) -> None:
    runs: list[FakeResident] = []

    async def start(root: Path, _image: str, run_id: str) -> FakeResident:
        run = FakeResident(run_id, root / "runs" / run_id)
        runs.append(run)
        return run

    async def scenario() -> None:
        factory = harbor_resident_episode_factory(
            tmp_path, start_resident=start,
            image_check=lambda _image: asyncio.sleep(0, result=True),
        )
        episode = await factory(101)
        session = episode.session
        view = await session.observe()
        assert view.capabilities[0]["name"] == VISUAL_REACH
        assert view.capabilities[0]["input_schema"]["required"] == ("target_y_px",)
        first = await session.act(
            VISUAL_REACH, {"target_y_px": 224}, request_id="first",
            expected_runtime_id=view.runtime_id,
        )
        finished_first = await session.wait(first, 2.0)
        second = await session.act(
            VISUAL_REACH, {"target_y_px": 216}, request_id="second",
            expected_runtime_id=view.runtime_id,
        )
        finished_second = await session.wait(second, 2.0)
        repeated = await session.act(VISUAL_REACH, {"target_y_px": 216}, request_id="second")
        assert finished_first.lifecycle == finished_second.lifecycle == Lifecycle.SUCCEEDED
        assert first.operation_id != second.operation_id
        assert repeated.operation_id == second.operation_id
        assert len(runs) == 1
        assert len(runs[0].requests) == 2
        assert [target for _, target in runs[0].requests] == [224.0, 216.0]
        assert finished_second.result["run_id"] == runs[0].run_id
        assert finished_second.runtime_id == view.runtime_id
        with pytest.raises(AdmissionError, match="runtime ID is no longer current"):
            await session.act(
                VISUAL_REACH, {"target_y_px": 220}, request_id="stale",
                expected_runtime_id="old-runtime",
            )
        await session.close()
        assert runs[0].stop_calls == 1

    asyncio.run(scenario())


def test_cancel_reconciles_and_unconfirmed_result_inhibits_motion(tmp_path: Path) -> None:
    run = FakeResident("resident-test", tmp_path / "runs" / "resident-test")
    run.pending = asyncio.Event()

    async def scenario() -> None:
        factory = harbor_resident_episode_factory(
            tmp_path, start_resident=lambda *_args: asyncio.sleep(0, result=run),
            image_check=lambda _image: asyncio.sleep(0, result=True),
        )
        episode = await factory(202)
        session = episode.session
        operation = await session.act(VISUAL_REACH, {"target_y_px": 225})
        await asyncio.sleep(0)
        await session.cancel(operation)
        result = await session.wait(operation, 2.0)
        assert result.lifecycle == Lifecycle.CANCELED
        assert run.canceled == [operation.operation_id]
        assert run.stop_calls == 0
        run.pending = None
        run.quiescence_confirmed = False
        next_operation = await session.act(VISUAL_REACH, {"target_y_px": 218})
        uncertain = await session.wait(next_operation, 2.0)
        assert uncertain.lifecycle == Lifecycle.INDETERMINATE
        assert (await session.observe()).motion_inhibited_reason == "STOP_UNCONFIRMED"
        with pytest.raises(AdmissionError, match="motion is inhibited"):
            await session.act(VISUAL_REACH, {"target_y_px": 219})
        await session.close()

    asyncio.run(scenario())
