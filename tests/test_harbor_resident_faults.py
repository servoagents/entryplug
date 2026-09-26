from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from entryplug.harbor_episode import StopReport
from entryplug.harbor_resident import (
    SOURCE_ID,
    SOURCE_LINEAGE,
    ResidentReplyLost,
    harbor_resident_episode_factory,
)
from entryplug.operation import AdmissionError, Lifecycle, MotionState
from entryplug.visual_task import VISUAL_REACH


@dataclass
class LostReplyRun:
    run_id: str
    evidence_path: Path
    stopped: bool
    ready: dict[str, object] = field(
        default_factory=lambda: {
            "source_id": SOURCE_ID,
            "lineage_id": SOURCE_LINEAGE,
            "initial_y_px": 240.0,
        }
    )
    stop_calls: int = 0

    async def request(self, _operation_id: str, _target_y_px: float) -> dict[str, object]:
        raise ResidentReplyLost("lost after native admission")

    async def cancel(self, _operation_id: str) -> None:
        return None

    async def stop(self) -> StopReport:
        self.stop_calls += 1
        return StopReport(self.stopped, False)


@pytest.mark.parametrize("stop_confirmed", [True, False])
def test_lost_reply_never_becomes_a_successful_visual_task(
    tmp_path: Path, stop_confirmed: bool
) -> None:
    run = LostReplyRun("lost-reply", tmp_path / "runs" / "lost-reply", stop_confirmed)

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
        assert result.lifecycle == (Lifecycle.FAILED if stop_confirmed else Lifecycle.INDETERMINATE)
        assert result.motion_state == (MotionState.IDLE if stop_confirmed else MotionState.UNKNOWN)
        assert run.stop_calls == 1
        if not stop_confirmed:
            assert (await session.observe()).motion_inhibited_reason == "STOP_UNCONFIRMED"
            with pytest.raises(AdmissionError, match="resident world is unavailable"):
                await session.act(VISUAL_REACH, {"target_y_px": 243})
            with pytest.raises(RuntimeError, match="stop could not be confirmed"):
                await session.close()
            run.stopped = True
            await session.close()
            assert run.stop_calls == 3
        else:
            with pytest.raises(AdmissionError, match="resident world is unavailable"):
                await session.act(VISUAL_REACH, {"target_y_px": 243})
            await session.close()

    asyncio.run(scenario())
