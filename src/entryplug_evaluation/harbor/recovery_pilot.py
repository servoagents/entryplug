"""Run the declared resident recovery development matrix without hiding failures."""

from __future__ import annotations

import json
import statistics
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from entryplug.core.evidence import json_object
from entryplug_evaluation.harbor.demo import ResidentDemoResult, run_resident_demo
from entryplug_evaluation.harbor.records import finite_float, nonnegative_int
from entryplug_evaluation.harbor.recovery import summarize_recovery_episode
from entryplug_harbor.recovery_qualification import HARBOR_RESIDENT_RECOVERY_V1 as PROFILE
from entryplug_harbor.resident import ResidentStartupFailure
from entryplug_harbor.runtime import DEFAULT_HARBOR_IMAGE

SCENARIOS: tuple[tuple[str, str | None], ...] = (
    ("no_fault", None),
    ("recoverable_loss", "kill-active-worker"),
    ("stale_replacement", "kill-active-worker-stale-alternate"),
)

EpisodeRunner = Callable[..., Awaitable[ResidentDemoResult]]
EpisodeScorer = Callable[..., dict[str, object]]
ProgressReporter = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class RecoveryPilotResult:
    run_id: str
    evidence_path: Path
    passed: bool
    trial_count: int
    gate_pass_count: int


def _median(rows: list[dict[str, object]], key: str) -> float | None:
    values: list[float] = []
    for row in rows:
        measurements = row.get("measurements")
        if not isinstance(measurements, Mapping):
            continue
        value = finite_float(measurements.get(key))
        if value is not None and value >= 0:
            values.append(value)
    return round(statistics.median(values), 6) if values else None


def _aggregate(rows: list[dict[str, object]]) -> dict[str, object]:
    counts: dict[str, object] = {}
    for scenario, _ in SCENARIOS:
        subset = [row for row in rows if row["scenario"] == scenario]
        passed = [row for row in subset if row["outcome"] == "passed"]
        counts[scenario] = {
            "trials": len(subset),
            "gate_passes": len(passed),
            "failures": sum(row["outcome"] == "failed" for row in subset),
            "aborts": sum(row["outcome"] == "aborted" for row in subset),
            "pre_trigger_failures": sum(
                row.get("reason_code") == "pre_trigger_failure" for row in subset
            ),
            "faults_applied": sum(row.get("fault_applied") is True for row in subset),
            "task_completions": sum(
                row["outcome"] == "passed"
                and row.get("passed_tasks") == len(PROFILE.base_offsets_px)
                for row in subset
            ),
            "false_completions": sum(
                nonnegative_int(row.get("false_completion_count")) for row in subset
            ),
            "gate_pass_conditioned_median_episode_wall_ms": _median(passed, "episode_wall_ms"),
            "gate_pass_conditioned_median_repair_ms": _median(passed, "repair_ms"),
        }
    return {
        "schema_version": 1,
        "phase": "development",
        "recorded_at": datetime.now(UTC).isoformat(),
        "qualification_profile": {"profile_id": PROFILE.profile_id, "digest": PROFILE.digest},
        "declared_seeds": list(PROFILE.development_seeds),
        "scenarios": [{"name": name, "fault": fault} for name, fault in SCENARIOS],
        "counts": counts,
        "trials": rows,
        "claim_boundary": (
            "Development trials vary only the declared image-row goals. "
            "These gates are not a held-out result or a comparison with handwritten recovery."
        ),
    }


async def run_recovery_development(
    root: Path,
    *,
    image: str = DEFAULT_HARBOR_IMAGE,
    episode_runner: EpisodeRunner = run_resident_demo,
    score_episode: EpisodeScorer = summarize_recovery_episode,
    report_progress: ProgressReporter | None = print,
) -> RecoveryPilotResult:
    """Run all nine declared development trials and save each result create-only."""

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"harbor-resident-pilot-development-{stamp}-{uuid.uuid4().hex[:8]}"
    evidence_path = root / "runs" / run_id
    evidence_path.mkdir(parents=True, exist_ok=False)
    rows: list[dict[str, object]] = []
    for seed in PROFILE.development_seeds:
        for scenario, fault in SCENARIOS:
            result: ResidentDemoResult | None = None
            row: dict[str, object]
            try:
                result = await episode_runner(
                    root,
                    seed=seed,
                    image=image,
                    evaluation_fault=fault,
                )
                row = dict(
                    json_object(
                        score_episode(result.evidence_path, seed=seed, fault=fault),
                        "recovery development trial",
                    )
                )
                row["demo_passed"] = result.passed
                expected_demo_pass = fault in {None, "kill-active-worker"}
                if row.get("outcome") == "passed" and result.passed != expected_demo_pass:
                    row["outcome"] = "failed"
                    row["reason_code"] = "demo_score_disagreement"
            except ResidentStartupFailure as error:
                row = {
                    "seed": seed,
                    "fault": fault,
                    "run_id": error.run_id,
                    "evidence_path": str(error.evidence_path),
                    "offered_tasks": len(PROFILE.base_offsets_px),
                    "outcome": "aborted",
                    "reason_code": "resident_startup_failed",
                    "error_type": type(error).__name__,
                    "error": str(error),
                    "stop_confirmed": error.stop_confirmed,
                    "process_exit_code": error.process_exit_code,
                }
            except (OSError, RuntimeError, ValueError) as error:
                row = {
                    "seed": seed,
                    "fault": fault,
                    "offered_tasks": len(PROFILE.base_offsets_px),
                    "outcome": "aborted",
                    "reason_code": "runtime_or_evidence_error",
                    "error_type": type(error).__name__,
                    "error": str(error),
                }
                if result is not None:
                    row["run_id"] = result.run_id
                    row["evidence_path"] = str(result.evidence_path)
            row["scenario"] = scenario
            rows.append(row)
            trial_path = evidence_path / f"seed-{seed}-{scenario}.json"
            with trial_path.open("x", encoding="utf-8") as stream:
                json.dump(row, stream, allow_nan=False, indent=2, sort_keys=True)
                stream.write("\n")
            if report_progress is not None:
                report_progress(
                    f"recovery development: seed {seed} {scenario} "
                    f"{row['outcome']} "
                    f"({len(rows)}/{len(PROFILE.development_seeds) * len(SCENARIOS)})"
                )

    with (evidence_path / "pilot.json").open("x", encoding="utf-8") as stream:
        json.dump(_aggregate(rows), stream, allow_nan=False, indent=2, sort_keys=True)
        stream.write("\n")
    gate_pass_count = sum(row["outcome"] == "passed" for row in rows)
    return RecoveryPilotResult(
        run_id=run_id,
        evidence_path=evidence_path,
        passed=gate_pass_count == len(rows),
        trial_count=len(rows),
        gate_pass_count=gate_pass_count,
    )
