"""Run and aggregate declared Hall of Mirrors evaluation suites."""

from __future__ import annotations

import json
import statistics
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from entryplug.qualification import HARBOR_MIRRORS_V1
from entryplug.runtime import DEFAULT_HARBOR_IMAGE, RuntimeResult, run_harbor

EpisodeRunner = Callable[..., RuntimeResult]
ProgressReporter = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class PilotResult:
    """Location and outcome of a complete declared seed suite."""

    run_id: str
    evidence_path: Path
    passed: bool
    episode_count: int
    passed_count: int


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _read_object(path: Path) -> Mapping[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def _read_evaluation(path: Path) -> Mapping[str, object]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) != 1:
        raise ValueError(f"{path.name} must contain exactly one record")
    value = json.loads(lines[0])
    if not isinstance(value, Mapping):
        raise ValueError(f"{path.name} record must be a JSON object")
    return value


def summarize_mirrors_episode(seed: int, result: RuntimeResult) -> dict[str, object]:
    """Reduce one episode without concealing missing or malformed evidence."""

    record: dict[str, object] = {
        "seed": seed,
        "run_id": result.run_id,
        "evidence_path": str(result.evidence_path),
        "returncode": result.returncode,
    }
    summary_path = result.evidence_path / "mirrors.json"
    if not summary_path.is_file():
        return {
            **record,
            "outcome": "failed",
            "reason_code": "missing_summary",
        }
    try:
        summary = _read_object(summary_path)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return {
            **record,
            "outcome": "failed",
            "reason_code": "invalid_summary",
            "error": str(error),
        }

    reported_status = summary.get("status")
    record["reported_status"] = reported_status
    if result.returncode != 0 or reported_status != "passed":
        return {
            **record,
            "outcome": "failed",
            "reason_code": "episode_failed",
            "error_type": summary.get("error_type"),
            "error": summary.get("error"),
        }

    evaluation_path = result.evidence_path / "evaluation.jsonl"
    try:
        evaluation = _read_evaluation(evaluation_path)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return {
            **record,
            "outcome": "failed",
            "reason_code": "invalid_private_evaluation",
            "error": str(error),
        }

    gates = _mapping(evaluation.get("gates"))
    checked_reuse = _mapping(summary.get("checked_reuse"))
    reuse_work = _mapping(checked_reuse.get("checked_reuse"))
    reacquisition = _mapping(checked_reuse.get("full_reacquisition"))
    comparison = _mapping(_mapping(summary.get("adaptive_handwritten_baseline")).get("comparison"))
    association = _mapping(summary.get("association"))
    timing = _mapping(summary.get("timing_ms"))
    metrics = {
        "total_duration_ms": _number(timing.get("total")),
        "probe_count": summary.get("probe_count"),
        "validation_probe_count": summary.get("validation_probe_count"),
        "reuse_probe_count": reuse_work.get("probe_count"),
        "reuse_duration_ms": _number(reuse_work.get("duration_ms")),
        "reacquisition_duration_ms": _number(reacquisition.get("duration_ms")),
        "reacquisition_command_travel_radians": _number(
            reacquisition.get("command_travel_radians")
        ),
        "wrapper_overhead_ms": _number(comparison.get("setup_time_delta_ms")),
    }
    selected_role = evaluation.get("selected_role")
    selected_controlled = (
        selected_role == "controlled_rendered_camera"
        and gates.get("controlled_source_selected") is True
    )
    stale_rejected = gates.get("delayed_path_rejected_as_stale") is True
    unrelated_rejected = gates.get("independent_source_rejected") is True
    passed = selected_controlled and stale_rejected and unrelated_rejected
    return {
        **record,
        "outcome": "passed" if passed else "failed",
        "reason_code": None if passed else "private_gate_failed",
        "selected_role": selected_role,
        "selected_candidate_id": association.get("candidate_id"),
        "selected_controlled_source": selected_controlled,
        "stale_path_rejected": stale_rejected,
        "unrelated_source_rejected": unrelated_rejected,
        "metrics": metrics,
    }


def _median(records: Sequence[dict[str, object]], metric: str) -> float | None:
    values: list[float] = []
    for record in records:
        metrics = _mapping(record.get("metrics"))
        value = _number(metrics.get(metric))
        if value is not None:
            values.append(value)
    return round(statistics.median(values), 6) if values else None


def _aggregate(
    phase: str,
    seeds: Sequence[int],
    episodes: Sequence[dict[str, object]],
) -> dict[str, object]:
    passed = [episode for episode in episodes if episode.get("outcome") == "passed"]
    failed = [episode for episode in episodes if episode.get("outcome") == "failed"]
    aborted = [episode for episode in episodes if episode.get("outcome") == "aborted"]
    wrong_source_count = sum(
        episode.get("selected_controlled_source") is False for episode in episodes
    )
    false_completion_count = sum(
        episode.get("reported_status") == "passed"
        and episode.get("selected_controlled_source") is False
        for episode in episodes
    )
    return {
        "schema_version": 1,
        "recorded_at": datetime.now(UTC).isoformat(),
        "phase": phase,
        "qualification_profile": {
            "profile_id": HARBOR_MIRRORS_V1.profile_id,
            "digest": HARBOR_MIRRORS_V1.digest,
        },
        "declared_seeds": list(seeds),
        "counts": {
            "episodes": len(episodes),
            "passed": len(passed),
            "failed": len(failed),
            "aborted": len(aborted),
            "wrong_source": wrong_source_count,
            "false_completion": false_completion_count,
        },
        "success_rate": round(len(passed) / len(episodes), 6),
        "medians": {
            "total_duration_ms": _median(passed, "total_duration_ms"),
            "reuse_duration_ms": _median(passed, "reuse_duration_ms"),
            "reacquisition_duration_ms": _median(passed, "reacquisition_duration_ms"),
            "wrapper_overhead_ms": _median(passed, "wrapper_overhead_ms"),
            "reacquisition_command_travel_radians": _median(
                passed, "reacquisition_command_travel_radians"
            ),
        },
        "episodes": list(episodes),
        "claim_boundary": (
            "This aggregate reports every declared seed in one frozen Harbor profile. "
            "It measures this fixture and does not establish general causal identification."
        ),
    }


def run_mirrors_pilot(
    root: Path,
    *,
    phase: str,
    image: str = DEFAULT_HARBOR_IMAGE,
    build: bool = False,
    episode_runner: EpisodeRunner = run_harbor,
    report_progress: ProgressReporter | None = print,
) -> PilotResult:
    """Run every seed in a frozen suite, continuing through episode failures."""

    seed_sets = {
        "development": HARBOR_MIRRORS_V1.development_seeds,
        "holdout": HARBOR_MIRRORS_V1.holdout_seeds,
    }
    if phase not in seed_sets:
        raise ValueError(f"unsupported pilot phase: {phase}")
    seeds = seed_sets[phase]
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"harbor-mirrors-pilot-{phase}-{stamp}-{uuid.uuid4().hex[:8]}"
    evidence_path = root / "runs" / run_id
    evidence_path.mkdir(parents=True, exist_ok=False)

    episodes: list[dict[str, object]] = []
    for index, seed in enumerate(seeds):
        try:
            result = episode_runner(
                root,
                image=image,
                case="mirrors",
                seed=seed,
                build=build and index == 0,
                capture_output=True,
            )
        except (OSError, RuntimeError, ValueError) as error:
            episode = {
                "seed": seed,
                "outcome": "aborted",
                "reason_code": "runtime_error",
                "error_type": type(error).__name__,
                "error": str(error),
            }
        else:
            episode = summarize_mirrors_episode(seed, result)
        episodes.append(episode)
        if report_progress is not None:
            report_progress(
                f"mirror pilot {phase}: seed {seed} {episode['outcome']} ({index + 1}/{len(seeds)})"
            )

    aggregate = _aggregate(phase, seeds, episodes)
    output = evidence_path / "pilot.json"
    with output.open("x", encoding="utf-8") as stream:
        json.dump(aggregate, stream, allow_nan=False, indent=2, sort_keys=True)
        stream.write("\n")
    passed_count = sum(episode.get("outcome") == "passed" for episode in episodes)
    return PilotResult(
        run_id=run_id,
        evidence_path=evidence_path,
        passed=passed_count == len(seeds),
        episode_count=len(seeds),
        passed_count=passed_count,
    )
