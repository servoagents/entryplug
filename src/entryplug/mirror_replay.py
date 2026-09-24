"""Load qualified Hall of Mirrors evidence for protocol replay."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from entryplug.association import CandidateEvidence, select_candidate
from entryplug.association_operation import candidate_from_record
from entryplug.qualification import HARBOR_MIRRORS_V1
from entryplug.seeding import validate_experiment_seed


@dataclass(frozen=True, slots=True)
class MirrorReplaySource:
    """Public replay input plus evaluator truth kept out of the agent request."""

    run_id: str
    evidence_path: Path
    seed: int
    candidates: tuple[CandidateEvidence, ...]
    recorded_candidate_id: str
    direct_candidate_id: str
    private_source_roles: Mapping[str, str]


def _object(path: Path) -> Mapping[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def _evaluation(path: Path) -> Mapping[str, object]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) != 1:
        raise ValueError(f"{path.name} must contain exactly one record")
    value = json.loads(lines[0])
    if not isinstance(value, Mapping):
        raise ValueError(f"{path.name} record must be a JSON object")
    return value


def _run_path(root: Path, run_id: str) -> Path:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", run_id) is None:
        raise ValueError("source run ID contains unsupported characters")
    runs = (root / "runs").resolve()
    source = (runs / run_id).resolve()
    if not source.is_relative_to(runs):
        raise ValueError("source run resolves outside the runs directory")
    return source


def load_mirror_replay_source(root: Path, run_id: str) -> MirrorReplaySource:
    """Validate one passed run and separate public evidence from private roles."""

    source = _run_path(root, run_id)
    summary_path = source / "mirrors.json"
    evaluation_path = source / "evaluation.jsonl"
    if not summary_path.is_file():
        raise FileNotFoundError(f"mirrors result is unavailable: {summary_path}")
    if not evaluation_path.is_file():
        raise FileNotFoundError(f"private evaluation is unavailable: {evaluation_path}")
    summary = _object(summary_path)
    evaluation = _evaluation(evaluation_path)
    if summary.get("status") != "passed":
        raise ValueError("source mirrors run did not pass qualification")

    qualification = summary.get("qualification_profile")
    if not isinstance(qualification, Mapping):
        raise ValueError("source mirrors run has no qualification profile")
    if (
        qualification.get("profile_id") != HARBOR_MIRRORS_V1.profile_id
        or qualification.get("digest") != HARBOR_MIRRORS_V1.digest
    ):
        raise ValueError("source mirrors run used a different qualification contract")

    seeds = summary.get("seeds")
    seed = seeds.get("experiment") if isinstance(seeds, Mapping) else None
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("source mirrors run has no valid experiment seed")
    validate_experiment_seed(seed)

    raw_candidates = summary.get("candidate_evidence")
    if not isinstance(raw_candidates, list) or not raw_candidates:
        raise ValueError("source mirrors run has no candidate evidence")
    candidates = tuple(candidate_from_record(item) for item in raw_candidates)

    association = summary.get("association")
    recorded_candidate = (
        association.get("candidate_id") if isinstance(association, Mapping) else None
    )
    if not isinstance(recorded_candidate, str) or not recorded_candidate:
        raise ValueError("source mirrors run has no recorded candidate selection")
    direct = select_candidate(candidates, HARBOR_MIRRORS_V1.association)
    direct_candidate = direct.get("candidate_id")
    if direct.get("status") != "selected" or not isinstance(direct_candidate, str):
        raise ValueError("current association contract refuses the source evidence")
    if direct_candidate != recorded_candidate:
        raise ValueError("current association result differs from the recorded selection")

    raw_roles = evaluation.get("private_source_roles")
    if not isinstance(raw_roles, Mapping) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in raw_roles.items()
    ):
        raise ValueError("source evaluation has no valid private role map")
    roles = {str(key): str(value) for key, value in raw_roles.items()}
    if recorded_candidate not in roles:
        raise ValueError("recorded selection is absent from the private role map")

    return MirrorReplaySource(
        run_id=run_id,
        evidence_path=source,
        seed=seed,
        candidates=candidates,
        recorded_candidate_id=recorded_candidate,
        direct_candidate_id=direct_candidate,
        private_source_roles=roles,
    )
