from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from entryplug.agent import Act, Cancel, Wait
from entryplug.episode import EpisodeController
from entryplug.harbor_episode import (
    ACQUIRE_VISUAL_BINDING,
    HarborRun,
    StopReport,
    harbor_mirrors_episode_factory,
)
from entryplug.operation import Lifecycle, MotionState
from entryplug.qualification import HARBOR_MIRRORS_V1


@dataclass
class _FakeRun(HarborRun):
    run_id: str
    evidence_path: Path
    completed: asyncio.Event
    returncode: int = 0
    stop_report: StopReport = StopReport(True, False)
    stop_calls: int = 0
    cancel_wait: bool = False

    async def wait(self) -> int:
        if self.cancel_wait:
            raise asyncio.CancelledError
        await self.completed.wait()
        return self.returncode

    async def stop(self) -> StopReport:
        self.stop_calls += 1
        if self.stop_report.confirmed:
            self.completed.set()
        return self.stop_report


def _write_passed_summary(path: Path, candidate_id: str = "view-a") -> None:
    path.mkdir(parents=True)
    (path / "mirrors.json").write_text(
        json.dumps(
            {
                "status": "passed",
                "association": {"candidate_id": candidate_id},
                "qualification_profile": {
                    "profile_id": HARBOR_MIRRORS_V1.profile_id,
                    "digest": HARBOR_MIRRORS_V1.digest,
                },
            }
        ),
        encoding="utf-8",
    )


def test_live_harbor_episode_returns_bounded_public_physical_result(tmp_path: Path) -> None:
    runs: list[_FakeRun] = []

    async def start(root: Path, image: str, run_id: str, seed: int) -> HarborRun:
        assert root == tmp_path
        assert image == "harbor:test"
        assert seed == 101
        evidence_path = root / "runs" / run_id
        _write_passed_summary(evidence_path)
        run = _FakeRun(run_id, evidence_path, asyncio.Event())
        run.completed.set()
        runs.append(run)
        return run

    async def image_check(image: str) -> bool:
        return image == "harbor:test"

    async def scenario() -> None:
        controller = EpisodeController(
            harbor_mirrors_episode_factory(
                tmp_path,
                image="harbor:test",
                start_harbor=start,
                image_check=image_check,
            ),
            timeout_seconds=10.0,
        )
        initial = await controller.reset(101)
        accepted = await controller.apply(
            Act(ACQUIRE_VISUAL_BINDING, {}),
            request_id="stable-live-request",
        )
        assert accepted is not None
        completed = await controller.apply(Wait(accepted.operation_id, 1.0))
        assert completed is not None

        assert completed.lifecycle == Lifecycle.SUCCEEDED
        assert completed.motion_state == MotionState.IDLE
        assert completed.result["selected_candidate_id"] == "view-a"
        assert completed.result["run_id"] == runs[0].run_id
        assert initial.public_metadata["fixture"] == "harbor_mirrors_live"
        assert "source_role" not in json.dumps(dict(completed.result))
        await controller.close()

    asyncio.run(scenario())


def test_episode_cancel_stops_owned_container_and_confirms_quiescence(tmp_path: Path) -> None:
    runs: list[_FakeRun] = []

    async def start(root: Path, _: str, run_id: str, __: int) -> HarborRun:
        run = _FakeRun(run_id, root / "runs" / run_id, asyncio.Event())
        runs.append(run)
        return run

    async def scenario() -> None:
        controller = EpisodeController(
            harbor_mirrors_episode_factory(
                tmp_path,
                start_harbor=start,
                image_check=lambda _: asyncio.sleep(0, result=True),
            ),
            timeout_seconds=10.0,
        )
        await controller.reset(202)
        accepted = await controller.apply(
            Act(ACQUIRE_VISUAL_BINDING, {}),
            request_id="cancel-live-request",
        )
        assert accepted is not None
        await asyncio.sleep(0)
        canceling = await controller.apply(Cancel(accepted.operation_id))
        assert canceling is not None and canceling.lifecycle == Lifecycle.CANCELING
        canceled = await controller.apply(Wait(accepted.operation_id, 1.0))
        assert canceled is not None

        assert canceled.lifecycle == Lifecycle.CANCELED
        assert canceled.motion_state == MotionState.IDLE
        assert canceled.result["quiescence_confirmed"] is True
        assert runs[0].stop_calls == 1
        await controller.close()

    asyncio.run(scenario())


def test_unconfirmed_stop_is_indeterminate_and_inhibits_motion(tmp_path: Path) -> None:
    async def start(root: Path, _: str, run_id: str, __: int) -> HarborRun:
        return _FakeRun(
            run_id,
            root / "runs" / run_id,
            asyncio.Event(),
            stop_report=StopReport(False, True),
        )

    async def scenario() -> None:
        start_episode = harbor_mirrors_episode_factory(
            tmp_path,
            start_harbor=start,
            image_check=lambda _: asyncio.sleep(0, result=True),
        )
        episode = await start_episode(303)
        operation = await episode.session.act(
            ACQUIRE_VISUAL_BINDING,
            {},
            request_id="uncertain-live-request",
        )
        await asyncio.sleep(0)
        await episode.session.cancel(operation)
        result = await episode.session.wait(operation, 1.0)

        assert result.lifecycle == Lifecycle.INDETERMINATE
        assert result.motion_state == MotionState.UNKNOWN
        assert result.result["quiescence_confirmed"] is False
        view = await episode.session.observe()
        assert view.motion_inhibited_reason == "STOP_UNCONFIRMED"
        await episode.session.close()

    asyncio.run(scenario())


def test_canceled_worker_still_stops_its_owned_container(tmp_path: Path) -> None:
    runs: list[_FakeRun] = []

    async def start(root: Path, _: str, run_id: str, __: int) -> HarborRun:
        run = _FakeRun(
            run_id,
            root / "runs" / run_id,
            asyncio.Event(),
            cancel_wait=True,
        )
        runs.append(run)
        return run

    async def scenario() -> None:
        start_episode = harbor_mirrors_episode_factory(
            tmp_path,
            start_harbor=start,
            image_check=lambda _: asyncio.sleep(0, result=True),
        )
        episode = await start_episode(404)
        operation = await episode.session.act(
            ACQUIRE_VISUAL_BINDING,
            {},
            request_id="canceled-worker-request",
        )
        result = await episode.session.wait(operation, 1.0)

        assert result.lifecycle == Lifecycle.CANCELED
        assert result.reason_code == "WORKER_CANCELED"
        assert result.result["quiescence_confirmed"] is True
        assert runs[0].stop_calls == 1
        await episode.session.close()

    asyncio.run(scenario())


def test_reset_refuses_when_selected_harbor_image_is_missing(tmp_path: Path) -> None:
    async def scenario() -> None:
        factory = harbor_mirrors_episode_factory(
            tmp_path,
            image="missing:test",
            image_check=lambda _: asyncio.sleep(0, result=False),
        )
        with pytest.raises(FileNotFoundError, match="build it before reset"):
            await factory(101)

    asyncio.run(scenario())
