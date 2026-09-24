from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from entryplug.pilot import run_mirrors_pilot
from entryplug.qualification import HARBOR_MIRRORS_V1
from entryplug.runtime import RuntimeResult


def _passed_episode(root: Path, seed: int) -> RuntimeResult:
    run_id = f"episode-{seed}"
    path = root / "runs" / run_id
    path.mkdir(parents=True)
    (path / "mirrors.json").write_text(
        json.dumps(
            {
                "status": "passed",
                "association": {"candidate_id": f"candidate-{seed}"},
                "timing_ms": {"total": 1000 + seed},
                "probe_count": 6,
                "validation_probe_count": 4,
                "checked_reuse": {
                    "checked_reuse": {"probe_count": 2, "duration_ms": 100},
                    "full_reacquisition": {
                        "duration_ms": 500,
                        "command_travel_radians": 0.35,
                    },
                },
                "adaptive_handwritten_baseline": {"comparison": {"setup_time_delta_ms": 8}},
            }
        ),
        encoding="utf-8",
    )
    (path / "evaluation.jsonl").write_text(
        json.dumps(
            {
                "selected_role": "controlled_rendered_camera",
                "gates": {
                    "controlled_source_selected": True,
                    "delayed_path_rejected_as_stale": True,
                    "independent_source_rejected": True,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return RuntimeResult(0, run_id, path)


def test_pilot_runs_every_declared_seed_and_aggregates_results(tmp_path: Path) -> None:
    observed: list[tuple[int, bool]] = []

    def runner(root: Path, **options: Any) -> RuntimeResult:
        seed = int(options["seed"])
        observed.append((seed, bool(options["build"])))
        return _passed_episode(root, seed)

    result = run_mirrors_pilot(
        tmp_path,
        phase="development",
        build=True,
        episode_runner=runner,
        report_progress=None,
    )

    assert result.passed
    assert observed == [(101, True), (202, False), (303, False)]
    aggregate = json.loads((result.evidence_path / "pilot.json").read_text())
    assert aggregate["declared_seeds"] == list(HARBOR_MIRRORS_V1.development_seeds)
    assert aggregate["counts"] == {
        "episodes": 3,
        "passed": 3,
        "failed": 0,
        "aborted": 0,
        "wrong_source": 0,
        "false_completion": 0,
    }
    assert aggregate["medians"]["reuse_duration_ms"] == 100
    assert aggregate["medians"]["reacquisition_duration_ms"] == 500


def test_pilot_preserves_failures_and_continues_after_runtime_errors(tmp_path: Path) -> None:
    calls: list[int] = []

    def runner(root: Path, **options: Any) -> RuntimeResult:
        seed = int(options["seed"])
        calls.append(seed)
        if seed == 101:
            raise RuntimeError("fixture did not start")
        if seed == 202:
            path = root / "runs" / "failed-202"
            path.mkdir(parents=True)
            (path / "mirrors.json").write_text(
                json.dumps({"status": "failed", "error": "association gates failed"}),
                encoding="utf-8",
            )
            return RuntimeResult(1, "failed-202", path)
        return _passed_episode(root, seed)

    result = run_mirrors_pilot(
        tmp_path,
        phase="development",
        episode_runner=runner,
        report_progress=None,
    )

    assert calls == [101, 202, 303]
    assert not result.passed
    aggregate = json.loads((result.evidence_path / "pilot.json").read_text())
    assert aggregate["counts"]["aborted"] == 1
    assert aggregate["counts"]["failed"] == 1
    assert [episode["outcome"] for episode in aggregate["episodes"]] == [
        "aborted",
        "failed",
        "passed",
    ]
