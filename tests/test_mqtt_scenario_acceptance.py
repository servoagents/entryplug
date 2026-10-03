"""MQTT qualification must retain native reads and actual rendered evidence."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from test_hybrid_session_acceptance import _evidence, _runner


def mqtt_runner(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(sys.modules, "run_hybrid", _runner())
    path = Path(__file__).resolve().parents[1] / "containers/scenarios/run_mqtt.py"
    spec = importlib.util.spec_from_file_location("entryplug_mqtt_acceptance", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def evidence(tmp_path: Path):
    root, _, report = _evidence(tmp_path)
    report["runtime_id"] = "mqtt-test"
    for op in report["operations"]:
        op["result"]["worker_id"] = "local-panel-worker-v1"
        for sample in op["result"]["verification"]["samples"]:
            for key in ("worker_generation", "job_id", "input_sha256"):
                sample[key] = None

    def failed(label: str) -> dict:
        return {
            "operation_id": label,
            "lifecycle": "failed",
            "effect_state": "none",
            "reason_code": "OBSERVATION_PROVIDER_FAILED",
            "result": {"lighting_writes": 0},
        }

    report["operations"] = [failed("before"), *report["operations"], failed("after")]
    report["bridge_join"] = {
        "runtime_id": "mqtt-test",
        "operation": report["operations"][0],
        "commands_sent": 0,
        "read_replies": 0,
        "retained_state": {"state": "ON", "brightness": 191},
    }
    report["mqtt_commands"] = [
        {
            "payload": {"state": "ON", "brightness": b},
            "retain": False,
            "qos": 0,
            "prior_revision": i + 1,
            "sent_monotonic": i + 1,
        }
        for i, b in enumerate((64, 128, 191))
    ]
    report["mqtt_reads"] = [
        {"nonce": str(i) * 32, **state, "requested_monotonic": i + 1, "received_monotonic": i + 1.1}
        for i, state in enumerate(report["applied_states"])
    ]
    (root / "bridge.json").write_text(
        json.dumps({"status": "stopped", "applied_count": 3, "rejected_commands": 0})
    )
    (root / "world-cleanup.json").write_text(json.dumps({"status": "stopped", "forced_stops": 0}))
    (root / "join-requested.json").write_text(json.dumps(report["bridge_join"]))
    return root, report


def test_complete_mqtt_evidence_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, report = evidence(tmp_path)
    (root / "mqtt.json").write_text(json.dumps(report))
    assert mqtt_runner(monkeypatch).check_result(root, "mqtt-test")["status"] == "passed"


@pytest.mark.parametrize(
    "mutation",
    [
        "retained_ready",
        "missing_read",
        "reused_nonce",
        "retained_command",
        "replayed_command",
        "old_revision",
        "wrong_worker",
        "invented_job",
        "warm_reuse",
        "missing_raw",
        "forced_stop",
    ],
)
def test_incomplete_mqtt_evidence_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    root, report = evidence(tmp_path)
    if mutation == "retained_ready":
        report["operations"][0]["lifecycle"] = "succeeded"
    elif mutation == "missing_read":
        report["mqtt_reads"].pop()
    elif mutation == "reused_nonce":
        report["mqtt_reads"][1]["nonce"] = report["mqtt_reads"][0]["nonce"]
    elif mutation == "retained_command":
        report["mqtt_commands"][0]["retain"] = True
    elif mutation == "replayed_command":
        report["mqtt_commands"].append(report["mqtt_commands"][0])
    elif mutation == "old_revision":
        report["mqtt_commands"][0]["prior_revision"] = 100
    elif mutation == "wrong_worker":
        report["operations"][1]["result"]["worker_id"] = "worker-a"
    elif mutation == "invented_job":
        report["operations"][1]["result"]["verification"]["samples"][0]["job_id"] = "native"
    elif mutation == "warm_reuse":
        report["operations"][2]["result"] = report["operations"][1]["result"]
    elif mutation == "missing_raw":
        (root / report["frame_artifacts"][0]).unlink()
    elif mutation == "forced_stop":
        (root / "world-cleanup.json").write_text('{"status":"stopped","forced_stops":1}')
    (root / "mqtt.json").write_text(json.dumps(report))
    runner = mqtt_runner(monkeypatch)
    with pytest.raises(sys.modules["run_hybrid"].ScenarioFailure):
        runner.check_result(root, "mqtt-test")
