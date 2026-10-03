#!/usr/bin/env python3
"""Owned MQTT lane: late bridge, applied lamp, local compute, fresh Session reuse."""

from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path

from run_hybrid import (
    ROOT,
    RUNS,
    OwnedStack,
    _check_inspection_evidence,
    _command,
    _prepare_images,
    _require,
)


class MqttStack(OwnedStack):
    def compose(self, *arguments: str, hybrid: bool = False, label: str) -> str:
        options = list(arguments)
        if options[:1] == ["up"]:
            options.extend(("--pull", "never"))
        return _command(
            [
                "docker",
                "compose",
                "-f",
                str(ROOT / "containers/scenarios/compose.mqtt.yaml"),
                *options,
            ],
            label,
            environment=self.environment,
        )


def check_result(run_dir: Path, run_id: str) -> dict[str, object]:
    report = json.loads((run_dir / "mqtt.json").read_text())
    _require(
        report.get("status") == "completed" and report.get("runtime_id") == run_id,
        "MQTT Session did not complete",
    )
    operations = report.get("operations")
    _require(
        isinstance(operations, list)
        and len(operations) == 4
        and all(isinstance(op, dict) for op in operations),
        "missing MQTT lifecycle evidence",
    )
    _require(len({op.get("operation_id") for op in operations}) == 4, "terminal operation reused")
    for index in (0, 3):
        op = operations[index]
        _require(
            op.get("lifecycle") == "failed"
            and op.get("effect_state") == "none"
            and op.get("reason_code") == "OBSERVATION_PROVIDER_FAILED"
            and op.get("result", {}).get("lighting_writes") == 0,
            "retained MQTT state established readiness without a bridge",
        )
    for index, writes in ((1, 3), (2, 0)):
        op = operations[index]
        _require(
            op.get("lifecycle") == "succeeded"
            and op.get("result", {}).get("lighting_writes") == writes,
            "joined bridge did not supply checked inspection and reuse",
        )
    _check_inspection_evidence(run_dir, {**report, "operations": operations[1:3]}, profile="mqtt")
    join = json.loads((run_dir / "join-requested.json").read_text())
    _require(
        join == report.get("bridge_join")
        and join.get("operation") == operations[0]
        and join.get("runtime_id") == run_id
        and join.get("commands_sent") == 0
        and join.get("read_replies") == 0
        and join.get("retained_state") == {"state": "ON", "brightness": 191},
        "bridge absence and retained state were not observed before join",
    )
    commands, reads = report.get("mqtt_commands"), report.get("mqtt_reads")
    _require(isinstance(commands, list) and len(commands) == 3, "MQTT command count mismatch")
    _require(isinstance(reads, list) and len(reads) >= 3, "missing live MQTT readbacks")
    nonces = [read.get("nonce") for read in reads]
    _require(
        len(set(nonces)) == len(nonces)
        and all(isinstance(n, str) and len(n) == 32 for n in nonces),
        "MQTT challenges reused or missing",
    )
    for cmd, brightness, state in zip(
        commands, (64, 128, 191), report["applied_states"], strict=True
    ):
        _require(
            cmd.get("payload") == {"state": "ON", "brightness": brightness}
            and cmd.get("retain") is False
            and cmd.get("qos") == 0,
            "MQTT command not bounded, absolute and non-retained",
        )
        _require(
            type(cmd.get("prior_revision")) is int and cmd["prior_revision"] < state["revision"],
            "command did not advance applied revision",
        )
        _require(
            any(
                read.get("revision") == state["revision"]
                and read.get("level") == state["level"]
                and read.get("sim_time_s") == state["sim_time_s"]
                and read.get("requested_monotonic", -1) >= cmd["sent_monotonic"]
                for read in reads
            ),
            "applied revision missing fresh MQTT readback",
        )
    bridge = json.loads((run_dir / "bridge.json").read_text())
    _require(
        bridge.get("status") == "stopped"
        and bridge.get("applied_count") == 3
        and bridge.get("rejected_commands") == 0,
        "bridge did not confirm and stop cleanly",
    )
    cleanup = json.loads((run_dir / "world-cleanup.json").read_text())
    _require(cleanup == {"status": "stopped", "forced_stops": 0}, "world did not stop cleanly")
    return {
        "status": "passed",
        "runtime_id": run_id,
        "operation_ids": [op["operation_id"] for op in operations],
        "task_outcomes": [op["lifecycle"] for op in operations],
        "task_lighting_writes": [0, 3, 0, 0],
        "dependencies": [
            "MQTT broker",
            "MQTT/ROS applied-state bridge",
            "ROS 2 camera",
            "MuJoCo panel",
            "local detector",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--build", action="store_true")
    args = parser.parse_args()
    run_id = "mqtt-" + uuid.uuid4().hex[:12]
    run_dir = RUNS / run_id
    RUNS.mkdir(exist_ok=True)
    outcome: dict[str, object] = {"status": "failed", "run_id": run_id}
    stack = None
    try:
        outcome.update(_prepare_images(build=args.build, native_worker=False))
        stack = MqttStack(f"ep-{run_id}", run_id, pull_images=False)
        stack.compose(
            "up", "-d", "--no-build", "broker", "workbench", label="starting owned MQTT fixture"
        )
        stack.compose(
            "exec",
            "-T",
            "workbench",
            "/usr/local/bin/entryplug-container",
            "bash",
            "/workspace/entryplug/containers/mixed/hybrid_inspection.sh",
            f"/workspace/entryplug/runs/{run_id}",
            run_id,
            "mqtt",
            label="qualifying MQTT bridge lifecycle",
        )
        outcome.update(check_result(run_dir, run_id))
    except Exception as error:
        outcome.update(status="failed", reason=type(error).__name__, detail=str(error))
    finally:
        if stack is not None:
            try:
                stack.close()
            except Exception as error:
                outcome.update(
                    status="failed", cleanup="failed", cleanup_reason=type(error).__name__
                )
        run_dir.mkdir(exist_ok=True)
        with (run_dir / "scenario.json").open("x") as stream:
            json.dump(outcome, stream, indent=2, allow_nan=False)
            stream.write("\n")
    print(json.dumps(outcome, sort_keys=True))
    return 0 if outcome["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
