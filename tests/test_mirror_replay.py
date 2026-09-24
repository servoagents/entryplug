from __future__ import annotations

import json
from pathlib import Path

import pytest

from entryplug.association import CandidateEvidence
from entryplug.association_operation import candidate_record
from entryplug.mirror_replay import load_mirror_replay_source
from entryplug.qualification import HARBOR_MIRRORS_V1


def _controlled() -> CandidateEvidence:
    return CandidateEvidence(
        candidate_id="view-public-a",
        lineage_id="lineage-public-a",
        maximum_age_ms=35.0,
        noise_range_px=0.8,
        fit_commands_radians=(-0.04, 0.025, -0.02, 0.05),
        fit_effects_px=(-3.25, 2.1, -1.7, 4.15),
        validation_commands_radians=(-0.045, 0.04),
        validation_effects_px=(-3.7, 3.35),
    )


def _source_run(root: Path, *, digest: str | None = None) -> str:
    run_id = "harbor-mirrors-source"
    path = root / "runs" / run_id
    path.mkdir(parents=True)
    controlled = _controlled()
    (path / "mirrors.json").write_text(
        json.dumps(
            {
                "status": "passed",
                "seeds": {"experiment": 101},
                "qualification_profile": {
                    "profile_id": HARBOR_MIRRORS_V1.profile_id,
                    "digest": digest or HARBOR_MIRRORS_V1.digest,
                },
                "candidate_evidence": [candidate_record(controlled)],
                "association": {"status": "selected", "candidate_id": controlled.candidate_id},
            }
        ),
        encoding="utf-8",
    )
    (path / "evaluation.jsonl").write_text(
        json.dumps(
            {
                "private_source_roles": {
                    controlled.candidate_id: "controlled_rendered_camera",
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return run_id


def test_replay_source_separates_candidate_evidence_from_private_roles(tmp_path: Path) -> None:
    run_id = _source_run(tmp_path)

    source = load_mirror_replay_source(tmp_path, run_id)

    assert source.recorded_candidate_id == "view-public-a"
    assert source.direct_candidate_id == "view-public-a"
    assert source.private_source_roles == {
        "view-public-a": "controlled_rendered_camera",
    }
    assert "controlled_rendered_camera" not in json.dumps(
        [candidate_record(item) for item in source.candidates]
    )


def test_replay_source_rejects_contract_drift_and_path_escape(tmp_path: Path) -> None:
    run_id = _source_run(tmp_path, digest="sha256:old-contract")

    with pytest.raises(ValueError, match="different qualification contract"):
        load_mirror_replay_source(tmp_path, run_id)
    with pytest.raises(ValueError, match="unsupported characters"):
        load_mirror_replay_source(tmp_path, "../outside")
