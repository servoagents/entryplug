"""Private late-join evaluator over a real camera and native detector Session."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

import rclpy
import zenoh
from panel_inspection import PanelFixture

from entryplug.core.evidence import json_object
from entryplug.core.operation import OperationHost
from entryplug.embodiment.inspection import CameraFrame, MarkerMeasurement, inspection_spec
from entryplug.harness.session import Session
from entryplug_zenoh.detector import ZenohDetectorWorker


class WorkerBinding:
    """Explicitly replaced only while the host's existing idle barrier is held."""

    worker_id = "worker-a"
    program_id = "fixed-red-centroid-v1"

    def __init__(self, worker: ZenohDetectorWorker) -> None:
        self.worker = worker

    async def detect(self, frame: CameraFrame) -> MarkerMeasurement:
        return await self.worker.detect(frame)


async def run(run_dir: Path, run_id: str) -> dict[str, object]:
    rclpy.init()
    camera = PanelFixture(run_dir)
    try:
        # Fixture setup before admission; the task itself has no lighting port.
        await camera.set_brightness(0.75)
        with zenoh.open(
            zenoh.Config.from_json5(
                json.dumps(
                    {
                        "mode": "client",
                        "scouting": {"multicast": {"enabled": False}},
                        "connect": {"endpoints": ["tcp/native-router:7448"]},
                    }
                )
            )
        ) as transport:
            binding = WorkerBinding(
                ZenohDetectorWorker(
                    transport,
                    run_id=run_id,
                    worker_id="worker-a",
                    generation=1,
                )
            )
            host = OperationHost((inspection_spec(camera, binding),), runtime_id=run_id)
            operations = []
            async with Session(host, owns_runtime=True) as session:

                async def inspect(label: str) -> None:
                    operation = await session.act(
                        "inspect_target",
                        {"target_id": "bench-marker"},
                        request_id=label,
                    )
                    await session.wait(operation, 35)
                    operations.append(
                        json_object(await session.inspect(operation, "result"), label)
                    )

                await inspect("before-worker-join")
                with (run_dir / "join-requested.json").open("x") as stream:
                    json.dump({"runtime_id": run_id, "operation": operations[-1]}, stream)
                deadline = time.monotonic() + 40
                while not (run_dir / "worker-joined.json").is_file():
                    if time.monotonic() >= deadline:
                        raise TimeoutError("supervisor did not start the approved worker")
                    await asyncio.sleep(0.05)
                with host.reconfiguration("compute", expected_runtime_id=run_id):
                    binding.worker.close()
                    binding.worker = ZenohDetectorWorker(
                        transport,
                        run_id=run_id,
                        worker_id="worker-a",
                        generation=1,
                    )
                    host.commit_reconfiguration("compute", expected_generation=1)
                await inspect("after-worker-join")
                await inspect("warm-worker")
                # A separate request with a stale expected worker identity must fail.
                # This evaluator-only fault never changes the detector algorithm.
                with host.reconfiguration("compute", expected_runtime_id=run_id):
                    binding.worker.close()
                    binding.worker = ZenohDetectorWorker(
                        transport,
                        run_id=run_id,
                        worker_id="worker-a",
                        generation=2,
                    )
                    host.commit_reconfiguration("compute", expected_generation=2)
                await inspect("wrong-worker-generation")
                binding.worker.close()
            return {
                "status": "completed",
                "runtime_id": run_id,
                "operations": operations,
                "applied_states": camera.applied_states,
                "frame_artifacts": camera.frame_artifacts,
                "frames": camera.frames,
                "fixture_lighting_writes": 1,
                "task_lighting_writes": 0,
                "reconfiguration_generation": host.reconfiguration_generation("compute"),
            }
    finally:
        camera.destroy_node()
        rclpy.shutdown()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    try:
        report = asyncio.run(run(args.run_dir, args.run_id))
    except Exception as error:
        report = {"status": "failed", "reason": type(error).__name__}
    with (args.run_dir / "zenoh.json").open("x") as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
        stream.write("\n")
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
