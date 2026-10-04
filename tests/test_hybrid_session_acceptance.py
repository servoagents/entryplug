"""The Session socket closing cleanly is not a physical-task pass."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest


def _runner() -> ModuleType:
    script = Path(__file__).resolve().parents[1] / "containers/scenarios/run_hybrid.py"
    spec = importlib.util.spec_from_file_location("entryplug_hybrid_acceptance", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_clean_session_close_cannot_mask_failed_inspection(tmp_path: Path) -> None:
    runner = _runner()
    token = tmp_path / "token"
    token.write_text("private-fixture-token", encoding="utf-8")
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    operations = [
        {
            "operation_id": f"op-{index}",
            "lifecycle": "failed" if index == 1 else "succeeded",
            "result": {
                "worker_id": "worker-a",
                "lighting_writes": 3 if index == 0 else 0,
                "sample_ids": [f"frame-{index}"],
            },
        }
        for index in range(2)
    ]
    (evidence / "hybrid.json").write_text(
        json.dumps({"status": "completed", "operations": operations}), encoding="utf-8"
    )
    (evidence / "bridge.json").write_text(
        json.dumps({"status": "stopped", "applied_count": 3}), encoding="utf-8"
    )
    with pytest.raises(runner.ScenarioFailure, match="fresh checked reuse"):
        runner._check_result(evidence, token)


def _evidence(tmp_path: Path) -> tuple[Path, Path, dict]:
    import hashlib

    token = tmp_path / "token"
    token.write_text("private-fixture-token", encoding="utf-8")
    root = tmp_path / "evidence"
    root.mkdir()
    frames = []
    operations = []
    for index in range(4):
        name = f"frame-{index}.png"
        content = b"\x89PNG\r\n\x1a\n" + bytes([index])
        (root / name).write_bytes(content)
        frames.append(
            {
                "sample_id": f"sample-{index}",
                "sequence": index + 7,
                "source_id": "panel-camera",
                "lineage_id": "panel-camera-rgb8-v1",
                "captured_sim_time_s": 4 + index,
                "received_monotonic": 10 + index,
                "artifact": name,
                "artifact_sha256": hashlib.sha256(content).hexdigest(),
                "input_sha256": "a" * 64,
            }
        )
    for index in range(2):
        samples = frames[index * 2 : index * 2 + 2]
        accepted = 11.1 + index * 2
        # Both local receptions fit the configured 500 ms budget.
        samples[0]["received_monotonic"] = accepted - 0.2
        samples[1]["received_monotonic"] = accepted - 0.1
        operations.append(
            {
                "operation_id": f"op-{index}",
                "lifecycle": "succeeded",
                "capability": "inspect_target",
                "motion_state": "idle",
                "effect_state": "reported" if index == 0 else "none",
                "result": {
                    "target_id": "bench-marker",
                    "source_id": "panel-camera",
                    "lineage_id": "panel-camera-rgb8-v1",
                    "program_id": "fixed-red-centroid-v1",
                    "worker_id": "worker-a",
                    "worker_replacements": 0,
                    "binding_revision": 1,
                    "quality": 0.72,
                    "centroid_px": [159.5, 118.1],
                    "lighting_writes": 3 if index == 0 else 0,
                    "last_reported_level": 0.75 if index == 0 else None,
                    "sample_ids": [f["sample_id"] for f in samples],
                    "last_sequence": samples[-1]["sequence"],
                    "verification": {
                        "accepted_monotonic": accepted,
                        "clock": "runtime_local_monotonic",
                        "maximum_frame_age_s": 0.5,
                        "minimum_quality": 0.5,
                        "samples": [
                            {
                                "sample_id": f["sample_id"],
                                "sequence": f["sequence"],
                                "received_monotonic": f["received_monotonic"],
                                "age_at_acceptance_s": accepted - f["received_monotonic"],
                                "quality": 0.72,
                                "worker_generation": 1,
                                "job_id": f"job-{f['sample_id']}",
                                "input_sha256": "a" * 64,
                            }
                            for f in samples
                        ],
                    },
                },
            }
        )
    report = {
        "status": "completed",
        "home_assistant_entity": "light.fixture",
        "operations": operations,
        "frames": frames,
        "frame_artifacts": [f["artifact"] for f in frames],
        "applied_states": [
            {"level": level, "revision": index + 2, "sim_time_s": index + 1}
            for index, level in enumerate([0.25, 0.5, 0.75])
        ],
    }
    (root / "bridge.json").write_text(json.dumps({"status": "stopped", "applied_count": 3}))
    (root / "worker-cleanup.json").write_text(
        json.dumps(
            {
                "status": "stopped",
                "workers": [{"worker_id": "worker-a", "exit_code": 0}],
            }
        )
    )
    (root / "client.json").write_text(json.dumps({"operations": operations}))
    (root / "hybrid.json").write_text(json.dumps(report))
    return root, token, report


def test_complete_inspection_evidence_is_accepted(tmp_path: Path) -> None:
    root, token, _ = _evidence(tmp_path)
    assert _runner()._check_result(root, token)["status"] == "passed"


@pytest.mark.parametrize(
    "mutation",
    [
        "empty",
        "duplicate",
        "backwards",
        "missing_raw",
        "wrong_hash",
        "escape_path",
        "wrong_target",
        "wrong_source",
        "wrong_program",
        "wrong_generation",
        "missing_job",
        "bad_quality",
        "bool_quality",
        "stale",
        "first_stale",
        "wrong_clock",
        "warm_reuse",
        "missing_application",
        "before_application",
        "duplicate_revision",
        "worker_running",
        "missing_worker_cleanup",
        "wrong_client",
        "wrong_effect",
        "malformed_operation",
    ],
)
def test_incomplete_or_contradictory_evidence_is_refused(tmp_path: Path, mutation: str) -> None:
    root, token, report = _evidence(tmp_path)
    first = report["operations"][0]["result"]
    verification = first["verification"]
    if mutation == "empty":
        first["sample_ids"] = []
    elif mutation == "duplicate":
        first["sample_ids"][1] = first["sample_ids"][0]
    elif mutation == "backwards":
        verification["samples"][1]["sequence"] = 0
    elif mutation == "missing_raw":
        (root / report["frames"][0]["artifact"]).unlink()
    elif mutation == "wrong_hash":
        report["frames"][0]["artifact_sha256"] = "0" * 64
    elif mutation == "escape_path":
        report["frames"][0]["artifact"] = "../token"
    elif mutation.startswith("wrong_") and mutation[6:] in {"target", "source", "program"}:
        first[mutation[6:] + "_id"] = "other"
    elif mutation == "wrong_generation":
        verification["samples"][0]["worker_generation"] = True
    elif mutation == "missing_job":
        verification["samples"][0].pop("job_id")
    elif mutation == "bad_quality":
        first["quality"] = float("nan")
    elif mutation == "bool_quality":
        first["quality"] = True
    elif mutation in {"stale", "first_stale"}:
        verification["samples"][0]["age_at_acceptance_s"] = 0.6
    elif mutation == "wrong_clock":
        verification["clock"] = "remote_monotonic"
    elif mutation == "warm_reuse":
        report["operations"][1]["result"]["sample_ids"] = first["sample_ids"]
    elif mutation == "missing_application":
        report["applied_states"] = []
    elif mutation == "before_application":
        report["frames"][0]["captured_sim_time_s"] = 0.0
    elif mutation == "duplicate_revision":
        report["applied_states"][1]["revision"] = 2
    elif mutation == "worker_running":
        (root / "worker-cleanup.json").write_text('{"status":"running"}')
    elif mutation == "missing_worker_cleanup":
        (root / "worker-cleanup.json").unlink()
    elif mutation == "wrong_client":
        (root / "client.json").write_text('{"operations":[]}')
    elif mutation == "wrong_effect":
        report["operations"][0]["effect_state"] = "none"
    elif mutation == "malformed_operation":
        report["operations"][0] = None
    else:
        raise AssertionError(mutation)
    (root / "hybrid.json").write_text(json.dumps(report))
    runner = _runner()
    with pytest.raises(runner.ScenarioFailure):
        runner._check_result(root, token)


def test_repair_requires_observed_fault_and_qualified_alternate(tmp_path: Path) -> None:
    root, token, report = _evidence(tmp_path)
    for op in report["operations"]:
        op["result"].update(worker_id="worker-b", binding_revision=2, worker_replacements=1)
        op["timing"] = {
            "accepted_monotonic": 1.0,
            "accepted_deadline_monotonic": 31.0,
            "final_deadline_monotonic": 31.0,
            "completed_monotonic": 14.0,
        }
    (root / "hybrid.json").write_text(json.dumps(report))
    (root / "worker-cleanup.json").write_text(
        json.dumps(
            {
                "status": "stopped",
                "workers": [
                    {"worker_id": "worker-a", "exit_code": 137},
                    {"worker_id": "worker-b", "exit_code": 0},
                ],
            }
        )
    )
    fault = {
        "phase": "before_native_reply",
        "job_number": 3,
        "signal": "SIGKILL",
        "result_identity": {
            "worker_id": "worker-a",
            "worker_generation": 1,
            "program_id": "fixed-red-centroid-v1",
            "sample_id": "sample-0",
        },
    }
    (root / "fault.json").write_text(json.dumps(fault))
    runner = _runner()
    assert runner._check_result(root, token, repair=True)["status"] == "passed"
    with pytest.raises(runner.ScenarioFailure):
        runner._check_result(root, token)
    fault["phase"] = "before_first_job"
    (root / "fault.json").write_text(json.dumps(fault))
    with pytest.raises(runner.ScenarioFailure, match="fault was not observed"):
        runner._check_result(root, token, repair=True)


def test_mission_evidence_must_join_native_operations_and_completion():
    import copy

    runner = _runner()
    report = {
        "mission": {"id": "mission"},
        "mission_runs": [
            {
                "id": str(i),
                "mission_id": "mission",
                "lifecycle": "completed",
                "model_calls": 0,
                "tool_calls": 1,
            }
            for i in range(2)
        ],
        "operations": [
            {"operation_id": str(i), "lifecycle": "succeeded", "result": {"sample": i}}
            for i in range(2)
        ],
        "application_operations": [
            {
                "id": f"app-{i}",
                "run_id": str(i),
                "native_operation_id": str(i),
                "lifecycle": "succeeded",
                "result": {"sample": i},
                "physical_effects": True,
                "evidence_ids": [str(i)],
            }
            for i in range(2)
        ],
        "mission_evidence": [
            {
                "id": str(i),
                "operation_id": f"app-{i}",
                "content": {
                    "operation_id": str(i),
                    "lifecycle": "succeeded",
                    "result": {"sample": i},
                },
            }
            for i in range(2)
        ],
    }
    runner._check_mission_evidence(report)
    for section, key, value in [
        ("mission_runs", "lifecycle", "active"),
        ("mission_runs", "model_calls", 1),
        ("mission_runs", "mission_id", "other"),
        ("application_operations", "native_operation_id", "wrong"),
        ("application_operations", "evidence_ids", []),
        ("mission_evidence", "content", {}),
    ]:
        changed = copy.deepcopy(report)
        changed[section][0][key] = value
        with pytest.raises(runner.ScenarioFailure):
            runner._check_mission_evidence(changed)
