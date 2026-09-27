from __future__ import annotations

import json
from pathlib import Path

import pytest

from entryplug.recovery_compare import (
    record_recovery_comparison,
    summarize_recovery_comparison,
)
from entryplug.recovery_qualification import HARBOR_RESIDENT_RECOVERY_V1 as PROFILE


def _run(index: int) -> str:
    return f"harbor-resident-20260927T000000Z-{index:08x}"


def _entry(index: int, scenario: str, path: str, *, seed: int = 101) -> dict[str, object]:
    return {
        "seed": seed,
        "scenario": scenario,
        "execution_path": path,
        "run_id": _run(index),
    }


def _manifest(*entries: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "phase": "development",
        "qualification_digest": PROFILE.digest,
        "attempts": list(entries),
    }


def test_comparison_reports_only_unique_passing_pairs(tmp_path: Path) -> None:
    entries = (
        _entry(1, "no_fault", "session"),
        _entry(2, "no_fault", "direct_handwritten"),
        _entry(3, "recoverable_loss", "session"),
        _entry(4, "recoverable_loss", "direct_handwritten"),
        _entry(5, "recoverable_loss", "direct_handwritten"),
    )
    statuses = {_run(5): "failed"}
    calls: list[tuple[int, str | None, str]] = []

    def score(
        path: Path, *, seed: int, fault: str | None, execution_path: str
    ) -> dict[str, object]:
        calls.append((seed, fault, execution_path))
        return {
            "outcome": statuses.get(path.name, "passed"),
            "measurements": {
                "episode_wall_ms": 100.0 if execution_path == "session" else 90.0,
                "container_startup_ms": 20.0 if execution_path == "session" else 15.0,
                "acquisition_ms": 30.0,
                "repair_ms": 10.0 if fault else None,
            },
        }

    report = summarize_recovery_comparison(tmp_path, _manifest(*entries), scorer=score)
    assert len(report["attempts"]) == 5
    assert report["paired_success_count"] == 1
    assert report["cells_with_both_paths"] == 2
    assert calls == [
        (101, None, "session"),
        (101, None, "direct_handwritten"),
        (101, "kill-active-worker", "session"),
        (101, "kill-active-worker", "direct_handwritten"),
        (101, "kill-active-worker", "direct_handwritten"),
    ]
    no_fault = report["cells"][0]
    assert no_fault["comparison_status"] == "paired_success"
    assert no_fault["timing"]["episode_wall_delta_ms_session_minus_direct"] == 10.0
    assert no_fault["timing"]["warm_delta_ms_session_minus_direct"] == 5.0
    recoverable = report["cells"][1]
    assert recoverable["comparison_status"] == "multiple_attempts"
    assert recoverable["timing"] is None
    assert recoverable["counts"]["direct_handwritten"] == {"attempts": 2, "passes": 1}
    assert report["cells"][2]["comparison_status"] == "missing_path"


def test_comparison_rejects_hidden_or_unqualified_trials(tmp_path: Path) -> None:
    def score(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"outcome": "passed"}

    with pytest.raises(ValueError, match="repeats a physical run"):
        summarize_recovery_comparison(
            tmp_path,
            _manifest(_entry(1, "no_fault", "session"), _entry(1, "no_fault", "session")),
            scorer=score,
        )
    with pytest.raises(ValueError, match="undeclared development seed"):
        summarize_recovery_comparison(
            tmp_path, _manifest(_entry(2, "no_fault", "session", seed=1109)), scorer=score
        )
    malicious = _entry(3, "no_fault", "session")
    malicious["run_id"] = "../outside"
    with pytest.raises(ValueError, match="invalid resident run ID"):
        summarize_recovery_comparison(tmp_path, _manifest(malicious), scorer=score)


def test_comparison_records_exact_manifest_and_result_create_only(tmp_path: Path) -> None:
    manifest = _manifest(_entry(1, "no_fault", "session"))
    manifest_path = tmp_path / "input.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    result = record_recovery_comparison(tmp_path, manifest_path)
    saved_input = json.loads((result.evidence_path / "manifest.json").read_text(encoding="utf-8"))
    saved_output = json.loads(
        (result.evidence_path / "comparison.json").read_text(encoding="utf-8")
    )
    assert saved_input == manifest
    assert saved_output["attempt_count"] == 1
    assert saved_output["paired_success_count"] == 0
    assert result.cell_count == 6


def test_unique_failed_pair_has_no_timing_delta(tmp_path: Path) -> None:
    def score(
        path: Path, *, seed: int, fault: str | None, execution_path: str
    ) -> dict[str, object]:
        return {
            "outcome": "failed" if execution_path == "direct_handwritten" else "passed",
            "measurements": {"episode_wall_ms": 90.0},
        }

    report = summarize_recovery_comparison(
        tmp_path,
        _manifest(
            _entry(1, "no_fault", "session"),
            _entry(2, "no_fault", "direct_handwritten"),
        ),
        scorer=score,
    )
    assert report["paired_success_count"] == 0
    assert report["cells"][0]["comparison_status"] == "mixed_outcome"
    assert report["cells"][0]["timing"] is None


def test_comparison_rejects_nonfinite_score_and_manifest(tmp_path: Path) -> None:
    def score(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"outcome": "passed", "measurements": {"episode_wall_ms": float("nan")}}

    with pytest.raises(ValueError, match="finite JSON-safe"):
        summarize_recovery_comparison(
            tmp_path, _manifest(_entry(1, "no_fault", "session")), scorer=score
        )

    manifest_path = tmp_path / "nonfinite-manifest.json"
    manifest_path.write_text('{"schema_version": NaN}', encoding="utf-8")
    with pytest.raises(ValueError, match="non-finite"):
        record_recovery_comparison(tmp_path, manifest_path)
    assert not (tmp_path / "runs").exists()
