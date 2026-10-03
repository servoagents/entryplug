"""The ROS scenario must score real faults and cannot reinterpret task refusal as success."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_scenario_image_identity import _runner as image_runner


def runner(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(sys.modules, "run_hybrid", image_runner())
    path = Path(__file__).resolve().parents[1] / "containers/scenarios/run_ros2_recovery.py"
    spec = importlib.util.spec_from_file_location("entryplug_ros2_recovery_scenario", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault,task_pass,score,expected",
    [
        (None, True, "passed", "passed"),
        (None, False, "passed", "failed"),
        ("kill-active-worker-stale-alternate", False, "passed", "passed"),
        ("kill-active-worker-stale-alternate", True, "passed", "failed"),
        ("kill-both-workers", False, "failed", "failed"),
    ],
)
async def test_scenario_requires_agreement_with_fault_scorer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fault, task_pass, score, expected
) -> None:
    module = runner(monkeypatch)

    async def demo(*args, **kwargs):
        assert kwargs["seed"] == 101
        assert kwargs["evaluation_fault"] == fault
        return SimpleNamespace(passed=task_pass, evidence_path=tmp_path)

    monkeypatch.setattr(module, "run_resident_demo", demo)
    monkeypatch.setattr(
        module, "summarize_recovery_episode", lambda *args, **kw: {"outcome": score}
    )
    monkeypatch.setattr(module, "check_world_removed", lambda path: None)
    row = await module.run_case("fixture", fault, seed=101)
    assert row["status"] == expected


@pytest.mark.asyncio
async def test_cleanup_failure_overrides_task_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = runner(monkeypatch)

    async def demo(*args, **kwargs):
        return SimpleNamespace(passed=True, evidence_path=tmp_path)

    def failed_cleanup(path):
        raise RuntimeError("owned world remains")

    monkeypatch.setattr(module, "run_resident_demo", demo)
    monkeypatch.setattr(
        module, "summarize_recovery_episode", lambda *args, **kw: {"outcome": "passed"}
    )
    monkeypatch.setattr(module, "check_world_removed", failed_cleanup)
    row = await module.run_case("no_fault", None, seed=101)
    assert row["status"] == "failed"
    assert row["detail"] == "owned world remains"


@pytest.mark.asyncio
async def test_all_declared_development_cases_are_retained_after_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = runner(monkeypatch)
    calls = []

    async def trial(name, fault, *, seed):
        calls.append((name, fault, seed))
        return {"case": name, "status": "failed" if len(calls) == 1 else "passed"}

    monkeypatch.setattr(module, "run_case", trial)
    rows = await module.run_cases(tmp_path)
    assert len(rows) == len(module.CASES) == 8
    assert calls == [(name, fault, 101) for name, fault in module.CASES]
    assert len(list(tmp_path.glob("*.json"))) == 8
    assert rows[0]["status"] == "failed"


def admitted_evidence(tmp_path: Path):
    operation = "a" * 32
    summary = {"operation_ids": ["b" * 32, operation], "runtime_id": "same-runtime"}
    goal = {
        "operation_id": operation,
        "runtime_id": "same-runtime",
        "run_id": tmp_path.name,
        "target_y_px": 220.5,
        "deadline_monotonic": 30.0,
    }
    task = {
        "operation_id": operation,
        "target_y_px": 220.5,
        "accepted_deadline_monotonic": 30.0,
        "completed_monotonic": 28.0,
    }
    (tmp_path / "resident-demo.json").write_text(json.dumps(summary))
    (tmp_path / f"goal-{operation}.json").write_text(json.dumps(goal))
    (tmp_path / "task-0002.json").write_text(json.dumps(task))
    return task


def test_repair_identity_uses_admitted_goal_and_deadline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    admitted_evidence(tmp_path)
    assert runner(monkeypatch).check_repair_identity(tmp_path)["deadline_monotonic"] == 30.0


@pytest.mark.parametrize(
    "key,value",
    [
        ("operation_id", "different"),
        ("target_y_px", 225),
        ("accepted_deadline_monotonic", 31),
        ("completed_monotonic", 31),
        ("completed_monotonic", float("nan")),
        ("accepted_deadline_monotonic", True),
    ],
)
def test_repair_identity_refuses_goal_or_budget_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, key, value
) -> None:
    task = admitted_evidence(tmp_path)
    task[key] = value
    (tmp_path / "task-0002.json").write_text(json.dumps(task))
    module = runner(monkeypatch)
    with pytest.raises(sys.modules["run_hybrid"].ScenarioFailure):
        module.check_repair_identity(tmp_path)
