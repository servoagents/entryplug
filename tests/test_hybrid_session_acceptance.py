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
