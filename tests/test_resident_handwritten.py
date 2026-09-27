from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from entryplug.harbor_episode import StopReport
from entryplug.harbor_resident import SOURCE_ID, SOURCE_LINEAGE, ResidentReplyLost
from entryplug.recovery_evaluation import summarize_recovery_episode
from entryplug.recovery_qualification import HARBOR_RESIDENT_RECOVERY_V1 as PROFILE
from entryplug.resident_handwritten import run_handwritten_recovery


@dataclass
class FakeDirectResident:
    run_id: str
    evidence_path: Path
    fail_at: int | None = None
    lose_reply_at: int | None = None
    stop_confirmed: bool = True
    unverified_at: int | None = None
    requests: list[tuple[str, float, str | None, float | None]] = field(default_factory=list)
    stop_calls: int = 0
    ready: dict[str, object] = field(
        default_factory=lambda: {
            "source_id": SOURCE_ID,
            "lineage_id": SOURCE_LINEAGE,
            "initial_y_px": 220.0,
            "acquisition_ms": 0.0,
        }
    )

    async def request(
        self,
        operation_id: str,
        target_y_px: float,
        *,
        evaluation_fault: str | None = None,
        evaluation_recovery_strategy: str = "checked_reuse",
        deadline_monotonic: float | None = None,
    ) -> dict[str, object]:
        assert evaluation_recovery_strategy == "checked_reuse"
        self.requests.append((operation_id, target_y_px, evaluation_fault, deadline_monotonic))
        index = len(self.requests)
        if self.lose_reply_at == index:
            raise ResidentReplyLost("reply was lost")
        passed = self.fail_at != index
        self.evidence_path.mkdir(parents=True, exist_ok=True)
        (self.evidence_path / f"task-{index:04d}.json").write_text(
            json.dumps(
                {
                    "operation_id": operation_id,
                    "status": "passed" if passed else "failed",
                    "quiescence_confirmed": True,
                    "binding_ready": passed,
                    "visual_capability_available": passed,
                    "fault_evaluator_only": {"applied": False},
                    "evaluator_only": {
                        "independently_inside_tolerance": passed,
                        "false_visual_completion": False,
                    },
                }
            ),
            encoding="utf-8",
        )
        return {
            "operation_id": operation_id,
            "status": "passed" if passed else "failed",
            "reason_code": "TARGET_REACHED" if passed else "VISUAL_CAPABILITY_UNAVAILABLE",
            "source_id": SOURCE_ID,
            "lineage_id": SOURCE_LINEAGE,
            "quiescence_confirmed": True,
            "binding_ready": passed and self.unverified_at != index,
            "visual_capability_available": passed,
        }

    async def cancel(self, operation_id: str) -> None:
        raise AssertionError(f"unexpected cancel: {operation_id}")

    async def stop(self) -> StopReport:
        self.stop_calls += 1
        return StopReport(self.stop_confirmed, False)


def test_direct_baseline_uses_same_seeded_targets_and_one_world(tmp_path: Path) -> None:
    runs: list[FakeDirectResident] = []

    async def start(root: Path, _image: str, run_id: str) -> FakeDirectResident:
        run = FakeDirectResident(run_id, root / "runs" / run_id)
        runs.append(run)
        return run

    async def scenario() -> None:
        result = await run_handwritten_recovery(
            tmp_path,
            seed=101,
            evaluation_fault=None,
            start_resident=start,
            image_check=lambda _image: asyncio.sleep(0, result=True),
        )
        run = runs[0]
        assert result.passed
        assert run.stop_calls == 1
        assert len(run.requests) == 4
        assert len({request[0] for request in run.requests}) == 4
        assert [request[1] for request in run.requests] == [
            round(220.0 + offset, 6) for offset in PROFILE.target_offsets(101)
        ]
        assert all(request[2] is None for request in run.requests)
        assert all(
            isinstance(request[3], float) and request[3] > time.monotonic()
            for request in run.requests
        )
        assert len(list(run.evidence_path.glob("goal-*.json"))) == 4
        summary = json.loads((run.evidence_path / "resident-demo.json").read_text(encoding="utf-8"))
        assert summary["execution_path"] == "direct_handwritten"
        assert summary["world_count"] == 1
        assert summary["agent_decision_count"] == 0
        assert summary["world_stop_confirmed"] is True
        scored = summarize_recovery_episode(
            run.evidence_path, seed=101, fault=None, execution_path="direct_handwritten"
        )
        assert scored["outcome"] == "passed"
        assert scored["execution_path"] == "direct_handwritten"
        assert (
            summarize_recovery_episode(run.evidence_path, seed=101, fault=None)["reason_code"]
            == "profile_or_trial_mismatch"
        )

    asyncio.run(scenario())


def test_direct_baseline_stops_after_failed_fault_task(tmp_path: Path) -> None:
    run = FakeDirectResident("direct-failure", tmp_path / "runs" / "direct-failure", fail_at=2)

    async def scenario() -> None:
        result = await run_handwritten_recovery(
            tmp_path,
            seed=202,
            evaluation_fault="kill-active-worker",
            start_resident=lambda *_args: asyncio.sleep(0, result=run),
            image_check=lambda _image: asyncio.sleep(0, result=True),
        )
        assert not result.passed
        assert len(run.requests) == 2
        assert run.requests[0][2] is None
        assert run.requests[1][2] == "kill-active-worker"
        assert run.stop_calls == 1
        summary = json.loads((run.evidence_path / "resident-demo.json").read_text(encoding="utf-8"))
        assert summary["status"] == "failed"
        assert summary["failure_reason"] == "VISUAL_CAPABILITY_UNAVAILABLE"

    asyncio.run(scenario())


def test_direct_baseline_does_not_retry_lost_reply(tmp_path: Path) -> None:
    run = FakeDirectResident("direct-lost", tmp_path / "runs" / "direct-lost", lose_reply_at=1)

    async def scenario() -> None:
        result = await run_handwritten_recovery(
            tmp_path,
            seed=303,
            start_resident=lambda *_args: asyncio.sleep(0, result=run),
            image_check=lambda _image: asyncio.sleep(0, result=True),
        )
        assert not result.passed
        assert len(run.requests) == 1
        assert run.stop_calls == 1
        summary = json.loads((run.evidence_path / "resident-demo.json").read_text(encoding="utf-8"))
        assert summary["failure_reason"] == "reply_lost:ResidentReplyLost"

    asyncio.run(scenario())


def test_direct_baseline_requires_confirmed_world_stop(tmp_path: Path) -> None:
    run = FakeDirectResident(
        "direct-unknown-stop",
        tmp_path / "runs" / "direct-unknown-stop",
        stop_confirmed=False,
    )

    async def scenario() -> None:
        result = await run_handwritten_recovery(
            tmp_path,
            seed=101,
            start_resident=lambda *_args: asyncio.sleep(0, result=run),
            image_check=lambda _image: asyncio.sleep(0, result=True),
        )
        assert not result.passed
        assert len(run.requests) == 4
        scored = summarize_recovery_episode(
            run.evidence_path,
            seed=101,
            fault=None,
            execution_path="direct_handwritten",
        )
        assert scored["outcome"] == "failed"
        assert scored["structural_gates_passed"] is False

    asyncio.run(scenario())


def test_direct_baseline_refuses_unverified_success(tmp_path: Path) -> None:
    run = FakeDirectResident(
        "direct-unverified",
        tmp_path / "runs" / "direct-unverified",
        unverified_at=1,
    )

    async def scenario() -> None:
        result = await run_handwritten_recovery(
            tmp_path,
            seed=101,
            start_resident=lambda *_args: asyncio.sleep(0, result=run),
            image_check=lambda _image: asyncio.sleep(0, result=True),
        )
        assert not result.passed
        assert len(run.requests) == 1
        assert run.stop_calls == 1
        summary = json.loads((run.evidence_path / "resident-demo.json").read_text(encoding="utf-8"))
        assert summary["failure_reason"] == "TASK_NOT_VERIFIED"

    asyncio.run(scenario())
