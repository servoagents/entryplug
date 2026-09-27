"""Explicit, create-only comparison of resident recovery development attempts."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from entryplug.recovery_evaluation import summarize_recovery_episode
from entryplug.recovery_qualification import HARBOR_RESIDENT_RECOVERY_V1 as PROFILE

SCENARIOS: Mapping[str, str | None] = {
    "no_fault": None,
    "recoverable_loss": "kill-active-worker",
}
PATHS = ("session", "direct_handwritten")
RUN_ID = re.compile(r"harbor-resident-\d{8}T\d{6}Z-[0-9a-f]{8}\Z")
EpisodeScorer = Callable[..., dict[str, object]]


@dataclass(frozen=True, slots=True)
class RecoveryComparison:
    run_id: str
    evidence_path: Path
    cell_count: int
    paired_success_count: int


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _warm_ms(measurements: Mapping[str, object]) -> float | None:
    wall = _number(measurements.get("episode_wall_ms"))
    startup = _number(measurements.get("container_startup_ms"))
    acquisition = _number(measurements.get("acquisition_ms"))
    if wall is None or startup is None or acquisition is None:
        return None
    return round(wall - startup - acquisition, 3)


def _difference(
    session: Mapping[str, object], direct: Mapping[str, object], key: str
) -> float | None:
    session_value = _number(session.get(key))
    direct_value = _number(direct.get(key))
    if session_value is None or direct_value is None:
        return None
    return round(session_value - direct_value, 3)


def _score(attempt: Mapping[str, object]) -> Mapping[str, object]:
    score = attempt.get("score")
    if not isinstance(score, Mapping):
        raise ValueError("comparison scorer did not return an object")
    return score


def _attempt(
    root: Path,
    entry: object,
    scorer: EpisodeScorer,
) -> dict[str, object]:
    if not isinstance(entry, dict):
        raise ValueError("comparison attempt must be an object")
    seed = entry.get("seed")
    scenario = entry.get("scenario")
    path = entry.get("execution_path")
    run_id = entry.get("run_id")
    if type(seed) is not int or seed not in PROFILE.development_seeds:
        raise ValueError("comparison attempt has an undeclared development seed")
    if not isinstance(scenario, str) or scenario not in SCENARIOS:
        raise ValueError("comparison attempt has an unsupported scenario")
    if path not in PATHS:
        raise ValueError("comparison attempt has an unsupported execution path")
    if not isinstance(run_id, str) or RUN_ID.fullmatch(run_id) is None:
        raise ValueError("comparison attempt has an invalid resident run ID")
    score = scorer(
        root / "runs" / run_id,
        seed=seed,
        fault=SCENARIOS[scenario],
        execution_path=path,
    )
    return {
        "seed": seed,
        "scenario": scenario,
        "execution_path": path,
        "run_id": run_id,
        "score": score,
    }


def summarize_recovery_comparison(
    root: Path,
    manifest: object,
    *,
    scorer: EpisodeScorer = summarize_recovery_episode,
) -> dict[str, object]:
    """Score every manifest attempt; never silently select a passing retry."""

    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("comparison manifest requires schema version 1")
    if manifest.get("phase") != "development":
        raise ValueError("comparison accepts only declared development trials")
    if manifest.get("qualification_digest") != PROFILE.digest:
        raise ValueError("comparison profile digest does not match the frozen profile")
    raw_attempts = manifest.get("attempts")
    if not isinstance(raw_attempts, list) or not raw_attempts:
        raise ValueError("comparison manifest has no attempts")

    attempts = [_attempt(root, item, scorer) for item in raw_attempts]
    run_ids = [item["run_id"] for item in attempts]
    if len(set(run_ids)) != len(run_ids):
        raise ValueError("comparison manifest repeats a physical run")

    cells: list[dict[str, object]] = []
    paired_success_count = 0
    cells_with_both_paths = 0
    for seed in PROFILE.development_seeds:
        for scenario in SCENARIOS:
            selected = [
                item for item in attempts if item["seed"] == seed and item["scenario"] == scenario
            ]
            by_path = {
                path: [item for item in selected if item["execution_path"] == path]
                for path in PATHS
            }
            counts = {
                path: {
                    "attempts": len(rows),
                    "passes": sum(_score(row).get("outcome") == "passed" for row in rows),
                }
                for path, rows in by_path.items()
            }
            if all(by_path[path] for path in PATHS):
                cells_with_both_paths += 1
            if any(not by_path[path] for path in PATHS):
                status = "missing_path"
            elif any(len(by_path[path]) != 1 for path in PATHS):
                status = "multiple_attempts"
            elif all(_score(by_path[path][0]).get("outcome") == "passed" for path in PATHS):
                status = "paired_success"
            else:
                status = "mixed_outcome"
            timing: dict[str, object] | None = None
            if status == "paired_success":
                paired_success_count += 1
                session = _score(by_path["session"][0]).get("measurements")
                direct = _score(by_path["direct_handwritten"][0]).get("measurements")
                if not isinstance(session, Mapping) or not isinstance(direct, Mapping):
                    raise ValueError("passing episode has no measurements")
                warm_session = _warm_ms(session)
                warm_direct = _warm_ms(direct)
                timing = {
                    "episode_wall_delta_ms_session_minus_direct": _difference(
                        session, direct, "episode_wall_ms"
                    ),
                    "startup_delta_ms_session_minus_direct": _difference(
                        session, direct, "container_startup_ms"
                    ),
                    "acquisition_delta_ms_session_minus_direct": _difference(
                        session, direct, "acquisition_ms"
                    ),
                    "warm_delta_ms_session_minus_direct": (
                        round(warm_session - warm_direct, 3)
                        if warm_session is not None and warm_direct is not None
                        else None
                    ),
                    "repair_delta_ms_session_minus_direct": _difference(
                        session, direct, "repair_ms"
                    ),
                }
            cells.append(
                {
                    "seed": seed,
                    "scenario": scenario,
                    "comparison_status": status,
                    "counts": counts,
                    "timing": timing,
                }
            )

    return {
        "schema_version": 1,
        "phase": "development",
        "recorded_at": datetime.now(UTC).isoformat(),
        "qualification_profile": {"profile_id": PROFILE.profile_id, "digest": PROFILE.digest},
        "declared_seeds": list(PROFILE.development_seeds),
        "attempt_count": len(attempts),
        "attempts": attempts,
        "cells": cells,
        "paired_success_count": paired_success_count,
        "cells_with_both_paths": cells_with_both_paths,
        "claim_boundary": (
            "Only manifest-listed development attempts are scored. Multiple attempts remain "
            "visible but have no paired timing delta. Success-conditioned timing is not a "
            "reliability or speedup claim; omitted local runs cannot be inferred."
        ),
    }


def record_recovery_comparison(root: Path, manifest_path: Path) -> RecoveryComparison:
    """Write the exact input and reduced result into a new ignored runs directory."""

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    summary = summarize_recovery_comparison(root, manifest)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"harbor-resident-compare-development-{stamp}-{uuid.uuid4().hex[:8]}"
    evidence_path = root / "runs" / run_id
    evidence_path.mkdir(parents=True, exist_ok=False)
    for name, payload in (("manifest.json", manifest), ("comparison.json", summary)):
        with (evidence_path / name).open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, allow_nan=False, indent=2, sort_keys=True)
            stream.write("\n")
    paired_count = summary["paired_success_count"]
    if not isinstance(paired_count, int):
        raise ValueError("comparison reducer returned an invalid pair count")
    return RecoveryComparison(
        run_id=run_id,
        evidence_path=evidence_path,
        cell_count=len(PROFILE.development_seeds) * len(SCENARIOS),
        paired_success_count=paired_count,
    )
