#!/usr/bin/python3
"""Rendered panel inspection through an independently running native worker."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import rclpy
import zenoh
from panel_inspection import PanelFixture

from entryplug.core.evidence import json_object
from entryplug.core.operation import Lifecycle, OperationHost
from entryplug.embodiment.inspection import inspection_spec
from entryplug.harness.session import Session
from entryplug_zenoh.detector import ZenohDetectorWorker


def _config(port: int) -> zenoh.Config:
    return zenoh.Config.from_json5(
        json.dumps(
            {
                "mode": "client",
                "scouting": {"multicast": {"enabled": False}},
                "connect": {"endpoints": [f"tcp/127.0.0.1:{port}"]},
            }
        )
    )


async def run(output: Path, run_id: str) -> dict[str, object]:
    rclpy.init()
    fixture = PanelFixture(output.parent)
    try:
        with (
            zenoh.open(_config(7448)) as primary_session,
            zenoh.open(_config(7449)) as alternate_session,
        ):
            primary = ZenohDetectorWorker(
                primary_session, run_id=run_id, worker_id="worker-a", generation=1
            )
            alternate = ZenohDetectorWorker(
                alternate_session, run_id=run_id, worker_id="worker-b", generation=1
            )
            host = OperationHost(
                (inspection_spec(fixture, primary, alternate_worker=alternate, light=fixture),),
                runtime_id=run_id,
            )
            session = Session(host, owns_runtime=True)
            try:
                operations: list[dict[str, object]] = []
                for request_id in (f"{run_id}-first", f"{run_id}-warm"):
                    operation = await session.act(
                        "inspect_target", {"target_id": "bench-marker"}, request_id=request_id
                    )
                    result = await session.wait(operation, 35.0)
                    record = json_object(
                        await session.inspect(operation.operation_id, detail="result"),
                        "native panel inspection",
                    )
                    operations.append(record)
                    if result.lifecycle != Lifecycle.SUCCEEDED:
                        break
                first = operations[0].get("result") if operations else None
                warm = operations[1].get("result") if len(operations) == 2 else None
                passed = (
                    isinstance(first, dict)
                    and isinstance(warm, dict)
                    and all(record.get("lifecycle") == "succeeded" for record in operations)
                    and operations[0].get("operation_id") != operations[1].get("operation_id")
                    and first.get("worker_id") == "worker-a"
                    and warm.get("worker_id") == "worker-a"
                    and first.get("lighting_writes") == 3
                    and warm.get("lighting_writes") == 0
                    and not set(first.get("sample_ids", [])) & set(warm.get("sample_ids", []))
                )
                return {
                    "status": "passed" if passed else "failed",
                    "fixture": "stationary rendered panel, no joints or arm commands",
                    "transport": "ROS camera -> native Zenoh peer -> Session inspect_target",
                    "operations": operations,
                    "applied_states": fixture.applied_states,
                    "frame_artifacts": fixture.frame_artifacts,
                }
            finally:
                await session.close()
                primary.close()
                alternate.close()
    finally:
        fixture.destroy_node()
        rclpy.shutdown()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    try:
        report = asyncio.run(run(args.output, args.run_id))
    except Exception as error:
        report = {"status": "failed", "reason": type(error).__name__, "detail": str(error)}
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
