from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from entryplug.harbor_episode import StopReport
from entryplug.harbor_resident import SOURCE_ID, SOURCE_LINEAGE, harbor_resident_episode_factory
from entryplug.operation import AdmissionError, Lifecycle, MotionState
from entryplug.visual_task import VISUAL_REACH


@dataclass
class WrongSourceRun:
    run_id: str
    evidence_path: Path
    ready: dict[str, object] = field(
        default_factory=lambda: {
            "source_id": SOURCE_ID,
            "lineage_id": SOURCE_LINEAGE,
            "initial_y_px": 240.0,
        }
    )

    async def request(self, operation_id: str, target_y_px: float) -> dict[str, object]:
        return {
            "version": 1,
            "type": "result",
            "operation_id": operation_id,
            "status": "passed",
            "source_id": "different-camera",
            "lineage_id": SOURCE_LINEAGE,
            "target_y_px": target_y_px,
            "final_y_px": target_y_px,
            "final_error_px": 0.0,
            "quiescence_confirmed": True,
        }

    async def cancel(self, _operation_id: str) -> None:
        return None

    async def stop(self) -> StopReport:
        return StopReport(True, False)


def test_wrong_source_cannot_complete_or_admit_later_motion(tmp_path: Path) -> None:
    run = WrongSourceRun("wrong-source", tmp_path / "runs" / "wrong-source")

    async def scenario() -> None:
        factory = harbor_resident_episode_factory(
            tmp_path,
            start_resident=lambda *_args: asyncio.sleep(0, result=run),
            image_check=lambda _image: asyncio.sleep(0, result=True),
        )
        episode = await factory(101)
        session = episode.session
        operation = await session.act(VISUAL_REACH, {"target_y_px": 244})
        result = await session.wait(operation, 2.0)
        assert result.lifecycle == Lifecycle.INDETERMINATE
        assert result.motion_state == MotionState.UNKNOWN
        assert result.reason_code == "SOURCE_IDENTITY_CHANGED"
        with pytest.raises(AdmissionError, match="motion is inhibited"):
            await session.act(VISUAL_REACH, {"target_y_px": 243})
        await session.close()

    asyncio.run(scenario())
