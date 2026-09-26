"""Four public image-row goals in one continuing Harbor episode."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from entryplug.agent import Act, AgentRunner, Decision, Stop, Wait
from entryplug.episode import EpisodeController
from entryplug.evidence import JsonValue, json_object
from entryplug.harbor_resident import harbor_resident_episode_factory
from entryplug.operation import Lifecycle, OperationSnapshot
from entryplug.runtime import DEFAULT_HARBOR_IMAGE
from entryplug.visual_task import VISUAL_REACH

OFFSETS_PX = (5.0, -5.0, 7.0, -7.0)


@dataclass(frozen=True, slots=True)
class ResidentDemoResult:
    passed: bool
    run_id: str
    evidence_path: Path
    runtime_id: str
    operation_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FourRowPolicy:
    """Use only the public operation view and four declared image-space goals."""

    targets: tuple[float, ...]

    def decide(self, view: Mapping[str, JsonValue]) -> Decision:
        operations = view["operations"]
        if not isinstance(operations, list):
            raise ValueError("agent view has no operation list")
        if operations:
            latest = operations[-1]
            if not isinstance(latest, dict):
                raise ValueError("agent operation is malformed")
            lifecycle = latest.get("lifecycle")
            if lifecycle in {"accepted", "running", "canceling"}:
                reference = latest.get("operation_id")
                if not isinstance(reference, str):
                    raise ValueError("agent operation has no ID")
                return Wait(reference, 2.0)
            if lifecycle != "succeeded":
                return Stop(f"visual task ended as {lifecycle}")
        if len(operations) >= len(self.targets):
            return Stop("four visual tasks complete")
        return Act(VISUAL_REACH, {"target_y_px": self.targets[len(operations)]})


async def run_resident_demo(
    root: Path, *, seed: int = 101, image: str = DEFAULT_HARBOR_IMAGE
) -> ResidentDemoResult:
    """Qualify sequential external tasks without a reset between targets."""

    controller = EpisodeController(
        harbor_resident_episode_factory(root, image=image), timeout_seconds=300.0
    )
    runner: AgentRunner | None = None
    started = time.monotonic()
    try:
        state = await controller.reset(seed)
        metadata = state.public_metadata
        initial_value = metadata["initial_y_px"]
        if isinstance(initial_value, bool) or not isinstance(initial_value, (int, float)):
            raise ValueError("resident initial row was not numeric")
        initial = float(initial_value)
        targets = tuple(round(initial + offset, 6) for offset in OFFSETS_PX)
        if any(not 0 <= target < 480 for target in targets):
            raise ValueError("fixture did not provide four image-space targets")
        runner = AgentRunner(
            FourRowPolicy(targets),
            decision_timeout_seconds=2.0,
            maximum_wait_seconds=3.0,
        )
        steps = await controller.run_until_stop(runner, maximum_decisions=120)
        accepted = tuple(
            cast(OperationSnapshot, step.result)
            for step in steps
            if isinstance(step.reply.decision, Act)
        )
        terminal = tuple(
            step.result
            for step in steps
            if isinstance(step.result, OperationSnapshot) and step.result.terminal
        )
        unique_terminal = {item.operation_id: item for item in terminal}
        passed = (
            len(accepted) == len(targets)
            and len(unique_terminal) == len(targets)
            and all(item.lifecycle == Lifecycle.SUCCEEDED for item in unique_terminal.values())
        )
        run_id = str(metadata["run_id"])
        evidence_path = root / "runs" / run_id
        evaluator_results: list[dict[str, object]] = []
        for index in range(1, len(accepted) + 1):
            artifact = evidence_path / f"task-{index:04d}.json"
            try:
                task_artifact = json.loads(artifact.read_text(encoding="utf-8"))
                score = task_artifact["evaluator_only"]
                if not isinstance(score, dict):
                    raise ValueError("private score is malformed")
                evaluator_results.append(score)
            except (OSError, KeyError, ValueError, json.JSONDecodeError):
                evaluator_results.append({"status": "missing_or_invalid", "task_index": index})
        independent_pass = (
            len(evaluator_results) == len(targets)
            and all(
                item.get("independently_inside_tolerance") is True for item in evaluator_results
            )
            and all(item.get("false_visual_completion") is False for item in evaluator_results)
        )
        passed = passed and independent_pass
        payload = json_object(
            {
                "schema_version": 1,
                "status": "passed" if passed else "failed",
                "run_id": run_id,
                "runtime_id": state.runtime_id,
                "episode_id": state.episode_id,
                "source_id": metadata["source_id"],
                "lineage_id": metadata["lineage_id"],
                "initial_y_px": initial,
                "targets_y_px": targets,
                "operation_ids": [item.operation_id for item in accepted],
                "request_ids": [item.request_id for item in accepted],
                "outcomes": [
                    {
                        "operation_id": item.operation_id,
                        "lifecycle": item.lifecycle.value,
                        "reason_code": item.reason_code,
                        "result": item.result,
                    }
                    for item in unique_terminal.values()
                ],
                "world_count": 1,
                "private_evaluator": {
                    "all_targets_inside_tolerance": independent_pass,
                    "results": evaluator_results,
                },
                "container_startup_and_acquisition_ms": metadata[
                    "container_startup_and_acquisition_ms"
                ],
                "container_startup_ms": metadata["container_startup_ms"],
                "acquisition_ms": metadata["acquisition_ms"],
                "episode_wall_ms": round((time.monotonic() - started) * 1000, 3),
                "agent_startup_ms": next(
                    (
                        step.reply.process_startup_ms
                        for step in steps
                        if step.reply.process_startup_ms is not None
                    ),
                    None,
                ),
                "agent_process_ids": sorted({step.reply.agent_process_id for step in steps}),
                "agent_decision_count": len(steps),
                "model_decision_count": 0,
                "claim_boundary": (
                    "One configured camera and local image-row goals. No multi-source association, "
                    "3D reach, or physical target identity is claimed."
                ),
            },
            "resident demo",
        )
        evidence_path.mkdir(parents=True, exist_ok=True)
        with (evidence_path / "resident-demo.json").open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
        return ResidentDemoResult(
            passed,
            run_id,
            evidence_path,
            state.runtime_id,
            tuple(item.operation_id for item in accepted),
        )
    finally:
        if runner is not None:
            await runner.close()
        await controller.close()
