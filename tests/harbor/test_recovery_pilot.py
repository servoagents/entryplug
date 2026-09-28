from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from entryplug_evaluation.harbor.demo import ResidentDemoResult
from entryplug_evaluation.harbor.recovery_pilot import SCENARIOS, run_recovery_development
from entryplug_harbor.recovery_qualification import HARBOR_RESIDENT_RECOVERY_V1 as PROFILE
from entryplug_harbor.resident import ResidentStartupFailure


def test_development_matrix_saves_every_declared_pair(tmp_path: Path) -> None:
    calls: list[tuple[int, str | None]] = []

    async def runner(root: Path, **options: Any) -> ResidentDemoResult:
        seed = options["seed"]
        fault = options["evaluation_fault"]
        calls.append((seed, fault))
        path = root / "runs" / f"fake-{len(calls)}"
        path.mkdir(parents=True)
        return ResidentDemoResult(
            fault in {None, "kill-active-worker"}, path.name, path, "runtime", ("op",)
        )

    def scorer(path: Path, *, seed: int, fault: str | None) -> dict[str, object]:
        return {
            "seed": seed,
            "fault": fault,
            "run_id": path.name,
            "outcome": "passed",
            "offered_tasks": 4,
            "passed_tasks": 2 if fault else 4,
            "fault_applied": fault is not None,
            "false_completion_count": 0,
            "measurements": {"episode_wall_ms": 1000.0, "repair_ms": 200.0 if fault else None},
        }

    result = asyncio.run(
        run_recovery_development(
            tmp_path, episode_runner=runner, score_episode=scorer, report_progress=None
        )
    )
    assert result.passed
    assert result.trial_count == result.gate_pass_count == 9
    assert calls == [(seed, fault) for seed in PROFILE.development_seeds for _, fault in SCENARIOS]
    aggregate = json.loads((result.evidence_path / "pilot.json").read_text())
    assert aggregate["phase"] == "development"
    assert aggregate["qualification_profile"]["digest"] == PROFILE.digest
    assert aggregate["declared_seeds"] == list(PROFILE.development_seeds)
    assert aggregate["counts"]["recoverable_loss"]["gate_passes"] == 3
    assert aggregate["counts"]["stale_replacement"]["task_completions"] == 0
    assert len(list(result.evidence_path.glob("seed-*.json"))) == 9


def test_development_matrix_keeps_pretrigger_failures_and_aborts(tmp_path: Path) -> None:
    calls = 0

    async def runner(root: Path, **_options: Any) -> ResidentDemoResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("container did not start")
        path = root / "runs" / f"fake-{calls}"
        path.mkdir(parents=True)
        seed = _options["seed"]
        fault = _options["evaluation_fault"]
        completed = fault in {None, "kill-active-worker"} and not (seed == 101 and fault)
        return ResidentDemoResult(completed, path.name, path, "runtime", ("op",))

    def scorer(path: Path, *, seed: int, fault: str | None) -> dict[str, object]:
        return {
            "seed": seed,
            "fault": fault,
            "run_id": path.name,
            "outcome": "failed" if seed == 101 and fault else "passed",
            "reason_code": "pre_trigger_failure" if seed == 101 and fault else None,
            "offered_tasks": 4,
            "passed_tasks": 0 if seed == 101 and fault else 4,
            "fault_applied": not (seed == 101 and fault),
            "false_completion_count": 0,
            "measurements": {"episode_wall_ms": 1000.0},
        }

    result = asyncio.run(
        run_recovery_development(
            tmp_path, episode_runner=runner, score_episode=scorer, report_progress=None
        )
    )
    aggregate = json.loads((result.evidence_path / "pilot.json").read_text())
    assert not result.passed
    assert result.trial_count == 9
    assert result.gate_pass_count == 6
    assert aggregate["counts"]["no_fault"]["aborts"] == 1
    assert aggregate["counts"]["recoverable_loss"]["pre_trigger_failures"] == 1
    assert aggregate["counts"]["stale_replacement"]["pre_trigger_failures"] == 1
    assert [row["outcome"] for row in aggregate["trials"][:3]] == [
        "aborted",
        "failed",
        "failed",
    ]


def test_development_matrix_links_startup_abort_to_owned_world(tmp_path: Path) -> None:
    calls = 0

    async def runner(root: Path, **options: Any) -> ResidentDemoResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ResidentStartupFailure(
                "failed-world",
                root / "runs" / "failed-world",
                "ResidentReplyLost: reply absent",
                stop_confirmed=True,
                process_exit_code=1,
            )
        path = root / "runs" / f"fake-{calls}"
        path.mkdir(parents=True)
        fault = options["evaluation_fault"]
        return ResidentDemoResult(
            fault in {None, "kill-active-worker"}, path.name, path, "runtime", ("op",)
        )

    def scorer(path: Path, *, seed: int, fault: str | None) -> dict[str, object]:
        return {
            "seed": seed,
            "fault": fault,
            "outcome": "passed",
            "passed_tasks": 4 if fault in {None, "kill-active-worker"} else 1,
            "false_completion_count": 0,
        }

    result = asyncio.run(
        run_recovery_development(
            tmp_path, episode_runner=runner, score_episode=scorer, report_progress=None
        )
    )
    row = json.loads((result.evidence_path / "seed-101-no_fault.json").read_text())
    assert row["outcome"] == "aborted"
    assert row["run_id"] == "failed-world"
    assert row["evidence_path"] == str(tmp_path / "runs" / "failed-world")
    assert row["process_exit_code"] == 1
    assert row["stop_confirmed"] is True
    assert result.trial_count == 9
    assert result.gate_pass_count == 8


def test_development_matrix_keeps_run_identity_when_score_is_invalid(tmp_path: Path) -> None:
    calls = 0

    async def runner(root: Path, **options: Any) -> ResidentDemoResult:
        nonlocal calls
        calls += 1
        path = root / "runs" / f"fake-{calls}"
        path.mkdir(parents=True)
        fault = options["evaluation_fault"]
        return ResidentDemoResult(
            fault in {None, "kill-active-worker"}, path.name, path, "runtime", ("op",)
        )

    def scorer(path: Path, *, seed: int, fault: str | None) -> dict[str, object]:
        return {
            "seed": seed,
            "fault": fault,
            "outcome": "passed",
            "measurements": {"episode_wall_ms": float("nan") if path.name == "fake-1" else 10.0},
        }

    result = asyncio.run(
        run_recovery_development(
            tmp_path, episode_runner=runner, score_episode=scorer, report_progress=None
        )
    )
    row = json.loads((result.evidence_path / "seed-101-no_fault.json").read_text())
    assert row["outcome"] == "aborted"
    assert row["reason_code"] == "runtime_or_evidence_error"
    assert row["run_id"] == "fake-1"
    assert row["evidence_path"] == str(tmp_path / "runs" / "fake-1")
    assert result.gate_pass_count == 8
