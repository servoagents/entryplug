#!/usr/bin/python3
"""One continuing panel task through HA lighting and a native Zenoh worker."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from entryplug.core.evidence import json_object
from entryplug.core.operation import Lifecycle
from hybrid_runtime import open_hybrid_runtime


async def run(output: Path, run_id: str, token_file: Path) -> dict[str, object]:
    async with open_hybrid_runtime(output.parent, run_id, token_file) as runtime:
        session = runtime.session
        operations: list[dict[str, object]] = []
        for request_id in (f"{run_id}-first", f"{run_id}-warm"):
            operation = await session.act(
                "inspect_target", {"target_id": "bench-marker"}, request_id=request_id
            )
            result = await session.wait(operation, 35.0)
            record = json_object(
                await session.inspect(operation.operation_id, detail="result"),
                "hybrid panel inspection",
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
            and first.get("worker_id") == warm.get("worker_id") == "worker-a"
            and first.get("lighting_writes") == 3
            and warm.get("lighting_writes") == 0
            and not set(first.get("sample_ids", [])) & set(warm.get("sample_ids", []))
        )
        return {
            "status": "passed" if passed else "failed",
            "transport": "HA WebSocket -> MQTT -> ROS/MuJoCo -> camera -> native Zenoh",
            "home_assistant_entity": runtime.light.entity_id,
            "operations": operations,
            "applied_states": runtime.camera.applied_states,
            "frame_artifacts": runtime.camera.frame_artifacts,
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--token-file", type=Path, default=Path("/run/secrets/ha-token"))
    args = parser.parse_args()
    try:
        report = asyncio.run(run(args.output, args.run_id, args.token_file))
    except Exception as error:
        report = {"status": "failed", "reason": type(error).__name__}
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
