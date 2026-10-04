"""Late ROS connection requires unchanged authority and retained visual evidence."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from test_ros2_recovery_scenario import runner as recovery_runner


def runner(monkeypatch):
    monkeypatch.setitem(sys.modules, "run_ros2_recovery", recovery_runner(monkeypatch))
    path = Path(__file__).resolve().parents[1] / "containers/scenarios/run_ros2_vision.py"
    spec = importlib.util.spec_from_file_location("entryplug_ros2_vision_scenario", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def evidence(root):
    acquired = {
        "status": "passed",
        "association": {"status": "selected"},
        "validation": {"status": "passed"},
        "binding": {"key": "sha256:binding"},
    }
    (root / "resident-acquisition.json").write_text(json.dumps(acquired))
    operations = []
    for index in (1, 2):
        operation = str(index) * 32
        result = {
            "run_id": root.name,
            "source_id": "camera-color-v1",
            "lineage_id": "camera-color-capture-v1",
            "binding_evidence_key": "sha256:binding",
            "detector_worker": {"frame_sequence": index * 10},
            "quiescence_confirmed": True,
            "binding_revision": 1,
            "validation": {"status": "reused"},
        }
        operations.append(
            {
                "operation_id": operation,
                "lifecycle": "succeeded",
                "motion_state": "holding",
                "result": result,
            }
        )
        task = {
            "operation_id": operation,
            "target_y_px": 220,
            "evaluator_only": {
                "independently_inside_tolerance": True,
                "false_visual_completion": False,
            },
            "raw_artifacts": {},
        }
        for suffix in ("before.raw.png", "after.raw.png"):
            path = root / f"task-{index:04d}-{suffix}"
            path.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes([index]))
            task["raw_artifacts"][path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        (root / f"task-{index:04d}.json").write_text(json.dumps(task))
        (root / f"goal-{operation}.json").write_text(
            json.dumps({"runtime_id": "same", "target_y_px": 220})
        )
    operations.append(
        {
            "operation_id": "3" * 32,
            "lifecycle": "failed",
            "reason_code": "REPLY_LOST_STOPPED",
            "result": {"quiescence_confirmed": True},
        }
    )
    return {
        "runtime_id": "same",
        "before_connect": {"runtime_id": "same", "available": False, "run_id": None},
        "after_connect": {
            "runtime_id": "same",
            "available": True,
            "run_id": root.name,
            "readiness_basis": "native_acquisition",
            "task_readiness": "requires_per_request_validation",
        },
        "after_loss": {"runtime_id": "same", "available": False},
        "initial_refusal": "WORLD_UNAVAILABLE",
        "initial_operations": 0,
        "post_loss_refusal": "WORLD_UNAVAILABLE",
        "operations": operations,
        "world_stop_confirmed": True,
        "world_removed": True,
        "missing_observed_monotonic": 1,
        "world_start_requested_monotonic": 2,
    }


def test_complete_visual_evidence_is_accepted(monkeypatch, tmp_path):
    runner(monkeypatch).check_session(evidence(tmp_path), tmp_path)


@pytest.mark.parametrize(
    "mutation",
    [
        "new_runtime",
        "early_start",
        "initial_dispatch",
        "false_ready",
        "missing_binding",
        "old_frame",
        "uncertain_stop",
        "lost_completion",
        "cleanup",
        "raw_hash",
        "raw_missing",
        "wrong_goal",
        "false_visual",
        "unsafe_id",
    ],
)
def test_visual_evaluator_rejects_incomplete_or_contradictory_evidence(
    monkeypatch, tmp_path, mutation
):
    report = evidence(tmp_path)
    if mutation == "new_runtime":
        report["after_connect"]["runtime_id"] = "another"
    elif mutation == "early_start":
        report["world_start_requested_monotonic"] = 0.5
    elif mutation == "initial_dispatch":
        report["initial_operations"] = 1
    elif mutation == "false_ready":
        report["before_connect"]["available"] = True
    elif mutation == "missing_binding":
        report["operations"][0]["result"].pop("binding_evidence_key")
    elif mutation == "old_frame":
        report["operations"][1]["result"]["detector_worker"]["frame_sequence"] = 10
    elif mutation == "uncertain_stop":
        report["operations"][2]["result"]["quiescence_confirmed"] = False
    elif mutation == "lost_completion":
        report["operations"][2]["lifecycle"] = "succeeded"
    elif mutation == "cleanup":
        report["world_removed"] = False
    elif mutation == "raw_hash":
        (tmp_path / "task-0002-after.raw.png").write_bytes(b"\x89PNG\r\n\x1a\nchanged")
    elif mutation == "raw_missing":
        (tmp_path / "task-0001-before.raw.png").unlink()
    elif mutation == "wrong_goal":
        p = tmp_path / ("goal-" + "1" * 32 + ".json")
        p.write_text(json.dumps({"runtime_id": "same", "target_y_px": 240}))
    elif mutation == "false_visual":
        p = tmp_path / "task-0001.json"
        task = json.loads(p.read_text())
        task["evaluator_only"]["false_visual_completion"] = True
        p.write_text(json.dumps(task))
    elif mutation == "unsafe_id":
        report["operations"][0]["operation_id"] = "../outside"
    module = runner(monkeypatch)
    with pytest.raises(sys.modules["run_hybrid"].ScenarioFailure):
        module.check_session(report, tmp_path)


@pytest.mark.asyncio
async def test_connection_preserves_task_failure_before_secondary_validation(monkeypatch, tmp_path):
    from types import SimpleNamespace

    module = runner(monkeypatch)
    joined = False
    closed = False
    native = tmp_path / "native"
    native.mkdir()

    class Client:
        async def inspect(self, item, *args):
            if item == "world":
                return {"runtime_id": "same", "available": joined}
            return {
                "operation_id": "1" * 32,
                "lifecycle": "failed",
                "reason_code": "TARGET_REFUSED",
            }

        async def observe(self):
            return SimpleNamespace(runtime_id="same", operations=[])

        async def act(self, capability, arguments, *, request_id):
            if request_id == "missing-world":
                raise module.AdmissionError("WORLD_UNAVAILABLE", "absent")
            return "operation"

        async def wait(self, *args):
            return SimpleNamespace(lifecycle=module.Lifecycle.FAILED)

        async def close(self):
            nonlocal closed
            closed = True

    client = Client()

    async def start(*args):
        return SimpleNamespace(evidence_path=native)

    def factory(root, *, before_connect, start_resident):
        async def episode(seed):
            nonlocal joined
            await before_connect(client)
            await start_resident()
            joined = True
            return SimpleNamespace(session=client, public_metadata={"initial_y_px": 240})

        return episode

    monkeypatch.setattr(module, "harbor_resident_episode_factory", factory)
    monkeypatch.setattr(module, "start_owned_resident", start)
    monkeypatch.setattr(module, "check_world_removed", lambda path: None)
    report = await module.run_connection(tmp_path)
    assert report["status"] == "failed"
    assert report["detail"] == "joined ROS visual goal did not succeed"
    assert (
        report["validation_failure"]["detail"]
        == "ROS runtime identity evidence is missing or changed"
    )
    assert report["world_removed"] is True
    assert closed
