#!/usr/bin/python3
"""Qualify locally learned image space reaching through public ROS interfaces."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import statistics
import time
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import rclpy
from action_msgs.msg import GoalStatus
from smoke import (
    JOINTS,
    Frame,
    Observer,
    _current_positions,
    _future,
    _goal_id,
    _send_goal,
    _spin_until,
    _wait_position,
    _wait_stationary,
    _write_create_only,
    _write_png_create_only,
)

from entryplug.agent import AgentRunner, ScriptedExplorer, Stop
from entryplug.evidence import JsonValue
from entryplug.operation import (
    CapabilitySpec,
    Lifecycle,
    MotionState,
    OperationContext,
    OperationHost,
    OperationResult,
    OperationSnapshot,
)
from entryplug.session import Session

CONTROL_JOINT = "joint2"
SUPPLIED_GAIN_PX_PER_RADIAN = 125.0
SUPPLIED_VISIBILITY_OFFSET_RADIANS = -0.065
FIT_PROBES_RADIANS = (0.025, -0.04, 0.035, -0.025)
VALIDATION_PROBES_RADIANS = (0.05, -0.045)
WARM_VALIDATION_PROBE_RADIANS = 0.02
HELD_OUT_OFFSETS_PX = (-6.0, 5.0, -7.0)
WARM_TARGET_OFFSET_PX = 6.0
MAX_ACTION_DELTA_RADIANS = 0.065
LOCAL_REQUEST_LIMIT_RADIANS = 0.12
TARGET_TOLERANCE_PX = 3.0
MIN_USEFUL_GAIN_PX_PER_RADIAN = 40.0


def _round(value: float) -> float:
    return round(value, 6)


def _marker(frame: Frame) -> dict[str, object]:
    if frame.encoding != "rgb8" or frame.step != frame.width * 3:
        raise RuntimeError(
            f"red marker detector requires packed rgb8, got {frame.encoding!r} "
            f"with step {frame.step}"
        )
    rgb = np.frombuffer(frame.data, dtype=np.uint8).reshape((frame.height, frame.width, 3))
    red = rgb[:, :, 0].astype(np.int16)
    green = rgb[:, :, 1].astype(np.int16)
    blue = rgb[:, :, 2].astype(np.int16)
    mask = (red >= 100) & (red >= green + 45) & (red >= blue + 45)
    rows, columns = np.nonzero(mask)
    if columns.size < 20:
        raise RuntimeError(f"red marker detector found only {columns.size} pixels")
    return {
        "x_px": _round(float(np.mean(columns))),
        "y_px": _round(float(np.mean(rows))),
        "area_px": int(columns.size),
        "bounding_box_px": {
            "left": int(np.min(columns)),
            "top": int(np.min(rows)),
            "right": int(np.max(columns)),
            "bottom": int(np.max(rows)),
        },
        "frame": frame.public_dict(),
    }


def _fresh_observation(
    node: Observer, *, after_sequence: int | None = None, samples: int = 3
) -> dict[str, object]:
    start_sequence = (
        after_sequence
        if after_sequence is not None
        else (node.frame.sequence if node.frame is not None else 0)
    )
    observations: list[dict[str, object]] = []
    frames: list[Frame] = []
    last_sequence = start_sequence
    deadline = time.monotonic() + 4.0
    while rclpy.ok() and time.monotonic() < deadline and len(observations) < samples:
        rclpy.spin_once(node, timeout_sec=0.1)
        frame = node.frame
        if frame is None or frame.sequence <= last_sequence:
            continue
        observations.append(_marker(frame))
        frames.append(frame)
        last_sequence = frame.sequence
    if len(observations) != samples:
        raise TimeoutError(f"received {len(observations)} of {samples} fresh camera samples")
    x_values = [float(item["x_px"]) for item in observations]
    y_values = [float(item["y_px"]) for item in observations]
    return {
        "x_px": _round(statistics.median(x_values)),
        "y_px": _round(statistics.median(y_values)),
        "sample_count": samples,
        "x_range_px": _round(max(x_values) - min(x_values)),
        "y_range_px": _round(max(y_values) - min(y_values)),
        "first_frame_sequence": observations[0]["frame"]["sequence"],
        "last_frame_sequence": observations[-1]["frame"]["sequence"],
        "last_frame": observations[-1]["frame"],
        "last_receive_age_ms": _round(
            max(0.0, (time.monotonic() - frames[-1].received_monotonic) * 1000)
        ),
        "marker_area_px": observations[-1]["area_px"],
        "bounding_box_px": observations[-1]["bounding_box_px"],
    }


def _execute_absolute(
    node: Observer,
    target: dict[str, float],
    *,
    purpose: str,
    trace: list[dict[str, object]],
    cancel_requested: Callable[[], bool] | None = None,
) -> dict[str, object]:
    before_positions = _current_positions(node)
    requested_delta = {name: float(target[name] - before_positions[name]) for name in JOINTS}
    maximum_delta = max(abs(value) for value in requested_delta.values())
    if maximum_delta > MAX_ACTION_DELTA_RADIANS + 1e-9:
        raise RuntimeError(
            f"{purpose} requested {maximum_delta:.6f} rad, beyond the per action bound"
        )

    before_feature = _fresh_observation(node)
    before_sequence = int(before_feature["last_frame_sequence"])
    request_id = uuid.uuid4().hex
    handle, submitted = _send_goal(node, target, 0.65)
    result_future = handle.get_result_async()
    native_cancel_requested = False
    cancel_acknowledged = False
    result_deadline = time.monotonic() + 10.0
    while rclpy.ok() and time.monotonic() < result_deadline and not result_future.done():
        rclpy.spin_once(node, timeout_sec=min(0.05, result_deadline - time.monotonic()))
        if cancel_requested is not None and cancel_requested() and not native_cancel_requested:
            native_cancel_requested = True
            cancel_response = _future(
                node,
                handle.cancel_goal_async(),
                5.0,
                f"{purpose} cancel acknowledgment",
            )
            cancel_acknowledged = bool(cancel_response.goals_canceling)
            if not cancel_acknowledged:
                raise RuntimeError(f"{purpose} action server did not acknowledge cancellation")
    if not result_future.done():
        raise TimeoutError(f"timed out waiting for {purpose} action result")
    error = result_future.exception()
    if error is not None:
        raise RuntimeError(f"{purpose} action result failed: {error}")
    result = result_future.result()
    native_result = time.monotonic()
    canceled = native_cancel_requested and result.status == GoalStatus.STATUS_CANCELED
    if result.status != GoalStatus.STATUS_SUCCEEDED and not canceled:
        raise RuntimeError(f"{purpose} action finished with status {result.status}")
    if not canceled:
        _wait_position(node, target, tolerance=0.008, timeout=4.0, label=purpose)
    stationary = _wait_stationary(node, timeout=3.0)
    after_positions = _current_positions(node)
    after_feature = _fresh_observation(node, after_sequence=before_sequence)
    observed_delta = {
        name: float(after_positions[name] - before_positions[name]) for name in JOINTS
    }
    item = {
        "sequence": len(trace) + 1,
        "request_id": request_id,
        "purpose": purpose,
        "goal_id": _goal_id(handle),
        "native_status": result.status,
        "cancel_requested": native_cancel_requested,
        "cancel_acknowledged": cancel_acknowledged,
        "stop_confirmed_from_feedback": canceled,
        "before_positions_radians": {name: _round(before_positions[name]) for name in JOINTS},
        "commanded_positions_radians": {name: _round(float(target[name])) for name in JOINTS},
        "requested_delta_radians": {name: _round(requested_delta[name]) for name in JOINTS},
        "after_positions_radians": {name: _round(after_positions[name]) for name in JOINTS},
        "observed_delta_radians": {name: _round(observed_delta[name]) for name in JOINTS},
        "before_feature": before_feature,
        "after_feature": after_feature,
        "observed_feature_delta_px": {
            "x": _round(float(after_feature["x_px"]) - float(before_feature["x_px"])),
            "y": _round(float(after_feature["y_px"]) - float(before_feature["y_px"])),
        },
        "stationary_from_public_feedback": stationary,
        "timing_ms": {
            "submitted_to_native_result": _round((native_result - submitted) * 1000),
            "submitted_to_post_motion_observation": _round((time.monotonic() - submitted) * 1000),
        },
        "application_timestamp_available": False,
    }
    trace.append(item)
    return item


def _establish_visibility_anchor(
    node: Observer, trace: list[dict[str, object]]
) -> tuple[dict[str, float], dict[str, object]]:
    before = _current_positions(node)
    target = {
        "joint1": before["joint1"],
        CONTROL_JOINT: before[CONTROL_JOINT] + SUPPLIED_VISIBILITY_OFFSET_RADIANS,
    }
    request_id = uuid.uuid4().hex
    handle, submitted = _send_goal(node, target, 0.65)
    result = _future(node, handle.get_result_async(), 10.0, "visibility anchor result")
    native_result = time.monotonic()
    if result.status != GoalStatus.STATUS_SUCCEEDED:
        raise RuntimeError(f"visibility anchor finished with status {result.status}")
    _wait_position(node, target, tolerance=0.008, timeout=4.0, label="visibility anchor")
    stationary = _wait_stationary(node, timeout=3.0)
    after = _current_positions(node)
    feature = _fresh_observation(node)
    item: dict[str, object] = {
        "sequence": len(trace) + 1,
        "request_id": request_id,
        "purpose": "supplied-visibility-anchor",
        "calibration_source": "supplied_fixture_bootstrap",
        "used_for_learned_fit": False,
        "goal_id": _goal_id(handle),
        "native_status": result.status,
        "before_positions_radians": {name: _round(before[name]) for name in JOINTS},
        "commanded_positions_radians": {name: _round(float(target[name])) for name in JOINTS},
        "requested_delta_radians": {name: _round(target[name] - before[name]) for name in JOINTS},
        "after_positions_radians": {name: _round(after[name]) for name in JOINTS},
        "observed_delta_radians": {name: _round(after[name] - before[name]) for name in JOINTS},
        "before_feature": {"available": False, "reason": "occluded_at_rest_pose"},
        "after_feature": feature,
        "stationary_from_public_feedback": stationary,
        "timing_ms": {
            "submitted_to_native_result": _round((native_result - submitted) * 1000),
            "submitted_to_post_motion_observation": _round((time.monotonic() - submitted) * 1000),
        },
        "application_timestamp_available": False,
    }
    trace.append(item)
    return after, item


def _target(anchor: dict[str, float], joint2: float) -> dict[str, float]:
    return {"joint1": anchor["joint1"], "joint2": joint2}


def _return_to_anchor(
    node: Observer,
    anchor: dict[str, float],
    *,
    purpose: str,
    trace: list[dict[str, object]],
) -> dict[str, object] | None:
    latest: dict[str, object] | None = None
    for index in range(3):
        current = _current_positions(node)
        if abs(current[CONTROL_JOINT] - anchor[CONTROL_JOINT]) <= 0.004:
            return latest
        step = max(
            -MAX_ACTION_DELTA_RADIANS,
            min(
                MAX_ACTION_DELTA_RADIANS,
                anchor[CONTROL_JOINT] - current[CONTROL_JOINT],
            ),
        )
        latest = _execute_absolute(
            node,
            _target(anchor, current[CONTROL_JOINT] + step),
            purpose=f"{purpose}:step-{index + 1}",
            trace=trace,
        )
    current = _current_positions(node)
    if abs(current[CONTROL_JOINT] - anchor[CONTROL_JOINT]) > 0.004:
        raise RuntimeError(f"{purpose} did not return to the public feedback anchor")
    return latest


def _measure_probe(
    node: Observer,
    anchor: dict[str, float],
    delta_radians: float,
    *,
    purpose: str,
    trace: list[dict[str, object]],
) -> dict[str, object]:
    _return_to_anchor(node, anchor, purpose=f"{purpose}:anchor", trace=trace)
    item = _execute_absolute(
        node,
        _target(anchor, anchor[CONTROL_JOINT] + delta_radians),
        purpose=purpose,
        trace=trace,
    )
    observed_y = float(item["observed_feature_delta_px"]["y"])
    measured = {
        "requested_delta_radians": _round(delta_radians),
        "observed_joint_delta_radians": item["observed_delta_radians"][CONTROL_JOINT],
        "observed_feature_delta_px": _round(observed_y),
        "goal_id": item["goal_id"],
        "request_id": item["request_id"],
    }
    _return_to_anchor(node, anchor, purpose=f"{purpose}:reset", trace=trace)
    return measured


def _fit_gain(probes: list[dict[str, object]]) -> dict[str, object]:
    commands = [float(probe["requested_delta_radians"]) for probe in probes]
    effects = [float(probe["observed_feature_delta_px"]) for probe in probes]
    denominator = sum(value * value for value in commands)
    if denominator <= 0:
        raise RuntimeError("probe set cannot identify a visual mapping")
    gain = (
        sum(command * effect for command, effect in zip(commands, effects, strict=True))
        / denominator
    )
    residuals = [effect - gain * command for command, effect in zip(commands, effects, strict=True)]
    if not math.isfinite(gain) or abs(gain) < MIN_USEFUL_GAIN_PX_PER_RADIAN:
        raise RuntimeError(f"learned visual gain is unusable: {gain}")
    return {
        "model": "delta_y_px = gain_px_per_radian * delta_joint2_radians",
        "gain_px_per_radian": _round(gain),
        "fit_rmse_px": _round(
            math.sqrt(sum(value * value for value in residuals) / len(residuals))
        ),
        "residuals_px": [_round(value) for value in residuals],
        "sample_count": len(probes),
        "signed_probe_coverage": min(commands) < 0 < max(commands),
    }


def _validate_gain(
    gain: float,
    probes: list[dict[str, object]],
    *,
    noise_range_px: float,
) -> dict[str, object]:
    errors = [
        float(probe["observed_feature_delta_px"]) - gain * float(probe["requested_delta_radians"])
        for probe in probes
    ]
    tolerance = max(2.5, noise_range_px * 2.0)
    maximum_error = max(abs(value) for value in errors)
    return {
        "status": "passed" if maximum_error <= tolerance else "failed",
        "not_used_for_fit": True,
        "prediction_errors_px": [_round(value) for value in errors],
        "maximum_absolute_error_px": _round(maximum_error),
        "tolerance_px": _round(tolerance),
    }


def _screen_request(axis: str, delta_px: float, gain: float) -> dict[str, object]:
    if axis != "y":
        return {
            "status": "refused",
            "reason_code": "unsupported_feature_axis",
            "requested_axis": axis,
            "supported_axes": ["y"],
        }
    required_radians = delta_px / gain
    if abs(required_radians) > LOCAL_REQUEST_LIMIT_RADIANS:
        return {
            "status": "refused",
            "reason_code": "outside_local_validity",
            "requested_axis": axis,
            "requested_delta_px": _round(delta_px),
            "estimated_required_radians": _round(required_radians),
            "local_limit_radians": LOCAL_REQUEST_LIMIT_RADIANS,
        }
    return {
        "status": "accepted",
        "requested_axis": axis,
        "requested_delta_px": _round(delta_px),
        "estimated_required_radians": _round(required_radians),
    }


def _reach_target(
    node: Observer,
    anchor: dict[str, float],
    *,
    gain: float,
    target_y_px: float,
    calibration_source: str,
    purpose: str,
    trace: list[dict[str, object]],
    cancel_requested: Callable[[], bool] | None = None,
) -> dict[str, object]:
    started = time.monotonic()
    initial = _fresh_observation(node)
    initial_error = target_y_px - float(initial["y_px"])
    screening = _screen_request("y", initial_error, gain)
    if screening["status"] == "refused":
        return {
            **screening,
            "commands_applied": 0,
            "calibration_source": calibration_source,
        }

    first_trace = len(trace)
    steps: list[dict[str, object]] = []
    observation = initial
    canceled = False
    for index in range(3):
        if cancel_requested is not None and cancel_requested():
            canceled = True
            break
        error = target_y_px - float(observation["y_px"])
        if abs(error) <= TARGET_TOLERANCE_PX:
            break
        requested_step = max(
            -MAX_ACTION_DELTA_RADIANS,
            min(MAX_ACTION_DELTA_RADIANS, error / gain),
        )
        current = _current_positions(node)
        proposed_joint2 = current[CONTROL_JOINT] + requested_step
        if abs(proposed_joint2 - anchor[CONTROL_JOINT]) > LOCAL_REQUEST_LIMIT_RADIANS:
            break
        item = _execute_absolute(
            node,
            _target(anchor, proposed_joint2),
            purpose=f"{purpose}:servo-step-{index + 1}",
            trace=trace,
            cancel_requested=cancel_requested,
        )
        observation = item["after_feature"]
        steps.append(
            {
                "index": index + 1,
                "error_before_px": _round(error),
                "requested_step_radians": _round(requested_step),
                "request_id": item["request_id"],
                "goal_id": item["goal_id"],
            }
        )
        if item["cancel_requested"]:
            canceled = True
            break

    if cancel_requested is not None and cancel_requested():
        canceled = True
    final_error = target_y_px - float(observation["y_px"])
    action_items = trace[first_trace:]
    travel = sum(
        abs(float(item["requested_delta_radians"][CONTROL_JOINT])) for item in action_items
    )
    return {
        "status": (
            "canceled"
            if canceled
            else "passed"
            if abs(final_error) <= TARGET_TOLERANCE_PX
            else "failed"
        ),
        "cancel_requested": canceled,
        "calibration_source": calibration_source,
        "target_y_px": _round(target_y_px),
        "initial_y_px": initial["y_px"],
        "final_y_px": observation["y_px"],
        "initial_error_px": _round(initial_error),
        "final_error_px": _round(final_error),
        "tolerance_px": TARGET_TOLERANCE_PX,
        "request_screening": screening,
        "steps": steps,
        "commands_applied": len(action_items),
        "commanded_travel_radians": _round(travel),
        "timing_ms": _round((time.monotonic() - started) * 1000),
    }


def _visual_reach_arguments(arguments: Mapping[str, JsonValue]) -> Mapping[str, object]:
    if set(arguments) != {"target_y_px"}:
        raise ValueError("visual reach requires exactly target_y_px")
    target = arguments["target_y_px"]
    if isinstance(target, bool) or not isinstance(target, (int, float)):
        raise ValueError("target_y_px must be a number")
    target_y_px = float(target)
    if not math.isfinite(target_y_px) or not 0 <= target_y_px < 480:
        raise ValueError("target_y_px must be finite and inside the 480 pixel image")
    return {"target_y_px": target_y_px}


async def _agent_reach(
    node: Observer,
    anchor: dict[str, float],
    *,
    gain: float,
    target_y_px: float,
    trace: list[dict[str, object]],
) -> tuple[dict[str, object], dict[str, object]]:
    """Run one real reach through the shared host, session, and child policy."""

    trial_result: dict[str, object] = {}

    async def run_visual_reach(
        context: OperationContext,
        arguments: Mapping[str, JsonValue],
    ) -> OperationResult:
        if context.cancel_requested:
            return OperationResult(
                Lifecycle.CANCELED,
                MotionState.HOLDING,
                reason_code="CANCELED_BEFORE_MOTION",
            )
        context.report("visual_servo", MotionState.MOVING)
        trial = await asyncio.to_thread(
            _reach_target,
            node,
            anchor,
            gain=gain,
            target_y_px=float(arguments["target_y_px"]),
            calibration_source="validated_cached_mapping",
            purpose="agent-warm-held-out",
            trace=trace,
            cancel_requested=lambda: context.cancel_requested,
        )
        trial_result.update(trial)
        context.report("reconcile", MotionState.HOLDING)
        if context.cancel_requested or trial["status"] == "canceled":
            return OperationResult(
                Lifecycle.CANCELED,
                MotionState.HOLDING,
                result=trial,
                reason_code="CANCEL_REQUESTED",
            )
        if trial["status"] != "passed":
            return OperationResult(
                Lifecycle.FAILED,
                MotionState.HOLDING,
                result=trial,
                reason_code="TARGET_NOT_REACHED",
            )
        return OperationResult(Lifecycle.SUCCEEDED, MotionState.HOLDING, result=trial)

    host = OperationHost(
        (
            CapabilitySpec(
                "visual_reach",
                "1",
                "Reach one image row using a freshly validated local visual mapping",
                True,
                30.0,
                10.0,
                _visual_reach_arguments,
                run_visual_reach,
            ),
        )
    )
    session = Session(host, owns_runtime=True)
    runner = AgentRunner(
        ScriptedExplorer(
            "visual_reach",
            {"target_y_px": target_y_px},
            wait_seconds=2.0,
        ),
        decision_timeout_seconds=2.0,
        maximum_wait_seconds=3.0,
    )
    try:
        steps = await runner.run_until_stop(session, maximum_decisions=20)
        view = await session.observe()
        if len(view.operations) != 1:
            raise RuntimeError("scripted reach did not produce exactly one operation")
        operation = view.operations[0]
        if operation.lifecycle != Lifecycle.SUCCEEDED or not trial_result:
            raise RuntimeError(
                f"agent visual reach finished as {operation.lifecycle.value}: "
                f"{operation.reason_code}"
            )
        decisions: list[dict[str, object]] = []
        for step in steps:
            item: dict[str, object] = {
                "decision_id": step.reply.decision_id,
                "kind": type(step.reply.decision).__name__.lower(),
                "agent_generation": step.reply.agent_generation,
                "agent_process_id": step.reply.agent_process_id,
                "process_startup_ms": step.reply.process_startup_ms,
                "decision_latency_ms": step.reply.decision_latency_ms,
            }
            if isinstance(step.result, OperationSnapshot):
                item["operation_lifecycle"] = step.result.lifecycle.value
                item["operation_phase"] = step.result.phase
            decisions.append(item)
        agent_process_ids = sorted({step.reply.agent_process_id for step in steps})
        execution = {
            "status": "passed",
            "policy": "deterministic_scripted",
            "policy_process_ids": agent_process_ids,
            "operation_process_id": os.getpid(),
            "separate_policy_process": all(value != os.getpid() for value in agent_process_ids),
            "decision_count": len(steps),
            "wait_decision_count": sum(
                type(step.reply.decision).__name__ == "Wait" for step in steps
            ),
            "decisions": decisions,
            "operation": {
                "operation_id": operation.operation_id,
                "request_id": operation.request_id,
                "runtime_id": operation.runtime_id,
                "capability": operation.capability,
                "lifecycle": operation.lifecycle.value,
                "motion_state": operation.motion_state.value,
                "reason_code": operation.reason_code,
            },
            "claim_boundary": (
                "The child policy selected public session decisions. Only the operation "
                "host admitted motion, generated the mutation ID, and reconciled the result."
            ),
        }
        if not execution["separate_policy_process"]:
            raise RuntimeError("scripted policy did not run in a separate process")
        if not isinstance(steps[-1].reply.decision, Stop):
            raise RuntimeError("scripted policy did not stop after the terminal result")
        return trial_result, execution
    finally:
        await runner.close()
        await session.close()


def _cost(trace: list[dict[str, object]], first: int, started: float) -> dict[str, object]:
    actions = trace[first:]
    return {
        "action_count": len(actions),
        "commanded_travel_radians": _round(
            sum(abs(float(item["requested_delta_radians"][CONTROL_JOINT])) for item in actions)
        ),
        "wall_time_ms": _round((time.monotonic() - started) * 1000),
    }


def _no_action_noise(node: Observer, samples: int = 8) -> dict[str, object]:
    values: list[dict[str, object]] = []
    for _ in range(samples):
        values.append(_fresh_observation(node, samples=1))
    x_values = [float(item["x_px"]) for item in values]
    y_values = [float(item["y_px"]) for item in values]
    return {
        "commands_applied": 0,
        "sample_count": samples,
        "x_range_px": _round(max(x_values) - min(x_values)),
        "y_range_px": _round(max(y_values) - min(y_values)),
        "y_population_stddev_px": _round(statistics.pstdev(y_values)),
        "first_frame_sequence": values[0]["last_frame_sequence"],
        "last_frame_sequence": values[-1]["last_frame_sequence"],
    }


def _refusals(gain: float, trace: list[dict[str, object]]) -> dict[str, object]:
    before = len(trace)
    horizontal = _screen_request("x", 5.0, gain)
    horizontal["commands_applied"] = 0
    requested_y_px = gain * (LOCAL_REQUEST_LIMIT_RADIANS + 0.08)
    outside = _screen_request("y", requested_y_px, gain)
    outside["commands_applied"] = 0
    refusals_correct = all(item["status"] == "refused" for item in (horizontal, outside))
    return {
        "status": "passed" if refusals_correct and len(trace) == before else "failed",
        "horizontal_target": horizontal,
        "outside_local_model": outside,
        "trace_entries_added": len(trace) - before,
    }


def _write_trace(path: Path, trace: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        for item in trace:
            stream.write(json.dumps(item, sort_keys=True))
            stream.write("\n")


def qualify(output_dir: Path, trace: list[dict[str, object]]) -> dict[str, object]:
    started = time.monotonic()
    node = Observer("entryplug_visual_reach")
    try:
        _spin_until(
            node,
            lambda: node.frame is not None and node.joints is not None,
            30.0,
            "camera and joint topics",
        )
        if not node.action.wait_for_server(timeout_sec=15.0):
            raise TimeoutError("trajectory action server was unavailable")
        assert node.frame is not None
        if node.frame.stamp_sec == 0 and node.frame.stamp_nanosec == 0:
            raise RuntimeError("camera frame used a zero timestamp")

        _write_png_create_only(output_dir / "reach-start.png", node.frame)
        anchor, visibility_anchor = _establish_visibility_anchor(node, trace)
        assert node.frame is not None
        _write_png_create_only(output_dir / "reach-anchor.png", node.frame)
        start_observation = _fresh_observation(node)
        noise = _no_action_noise(node)

        supplied_start = _fresh_observation(node)
        supplied = _reach_target(
            node,
            anchor,
            gain=SUPPLIED_GAIN_PX_PER_RADIAN,
            target_y_px=float(supplied_start["y_px"]) - 6.0,
            calibration_source="supplied_prior",
            purpose="supplied-calibration-check",
            trace=trace,
        )
        _return_to_anchor(node, anchor, purpose="supplied-calibration:reset", trace=trace)

        cold_first = len(trace)
        cold_started = time.monotonic()
        fit_probes = [
            _measure_probe(
                node,
                anchor,
                delta,
                purpose=f"fit-probe-{index + 1}",
                trace=trace,
            )
            for index, delta in enumerate(FIT_PROBES_RADIANS)
        ]
        fitted = _fit_gain(fit_probes)
        learned_gain = float(fitted["gain_px_per_radian"])
        validation_probes = [
            _measure_probe(
                node,
                anchor,
                delta,
                purpose=f"validation-probe-{index + 1}",
                trace=trace,
            )
            for index, delta in enumerate(VALIDATION_PROBES_RADIANS)
        ]
        validation = _validate_gain(
            learned_gain,
            validation_probes,
            noise_range_px=float(noise["y_range_px"]),
        )
        cold_cost = _cost(trace, cold_first, cold_started)
        if validation["status"] != "passed":
            raise RuntimeError(
                "learned image space mapping failed separate validation: "
                f"{validation['maximum_absolute_error_px']} px"
            )

        held_out: list[dict[str, object]] = []
        for index, offset in enumerate(HELD_OUT_OFFSETS_PX):
            _return_to_anchor(node, anchor, purpose=f"held-out-{index + 1}:anchor", trace=trace)
            reference = _fresh_observation(node)
            trial = _reach_target(
                node,
                anchor,
                gain=learned_gain,
                target_y_px=float(reference["y_px"]) + offset,
                calibration_source="probe_acquired",
                purpose=f"held-out-{index + 1}",
                trace=trace,
            )
            trial["held_out_from_fit"] = True
            trial["requested_offset_px"] = offset
            held_out.append(trial)
        _return_to_anchor(node, anchor, purpose="held-out:reset", trace=trace)

        warm_first = len(trace)
        warm_started = time.monotonic()
        warm_probe = _measure_probe(
            node,
            anchor,
            WARM_VALIDATION_PROBE_RADIANS,
            purpose="warm-cache-validation",
            trace=trace,
        )
        warm_validation = _validate_gain(
            learned_gain,
            [warm_probe],
            noise_range_px=float(noise["y_range_px"]),
        )
        warm_validation_cost = _cost(trace, warm_first, warm_started)
        if warm_validation["status"] != "passed":
            raise RuntimeError("cached mapping failed its fresh warm validation probe")
        warm_reference = _fresh_observation(node)
        warm_trial, agent_execution = asyncio.run(
            _agent_reach(
                node,
                anchor,
                gain=learned_gain,
                target_y_px=float(warm_reference["y_px"]) + WARM_TARGET_OFFSET_PX,
                trace=trace,
            )
        )
        warm_trial["held_out_from_fit"] = True
        warm_trial["requested_offset_px"] = WARM_TARGET_OFFSET_PX

        refusals = _refusals(learned_gain, trace)
        end_observation = _fresh_observation(node)
        assert node.frame is not None
        _write_png_create_only(output_dir / "reach-end.png", node.frame)

        reaching_passed = all(item["status"] == "passed" for item in held_out)
        overall_passed = (
            reaching_passed and warm_trial["status"] == "passed" and refusals["status"] == "passed"
        )
        if not overall_passed:
            raise RuntimeError("one or more held out reaching or refusal cases failed")
        return {
            "schema_version": 1,
            "status": "passed",
            "recorded_at": datetime.now(UTC).isoformat(),
            "interfaces": {
                "camera_topic": "/camera/color/image_raw",
                "joint_topic": "/joint_states",
                "trajectory_action": "/trajectory_controller/follow_joint_trajectory",
            },
            "feature": {
                "description": "centroid of pixels passing a fixed red color rule",
                "controlled_coordinate": "y_px",
                "unsupported_coordinate": "x_px",
                "start": start_observation,
                "end": end_observation,
                "no_action_noise": noise,
            },
            "calibration_lifecycle": {
                "visibility_anchor": visibility_anchor,
                "supplied_prior": {
                    "gain_px_per_radian": SUPPLIED_GAIN_PX_PER_RADIAN,
                    "used_for_learned_fit": False,
                    "reach_check": supplied,
                },
                "replacement": "probe_acquired_mapping",
            },
            "cold_calibration": {
                "fit_probes": fit_probes,
                "mapping": fitted,
                "separate_validation_probes": validation_probes,
                "validation": validation,
                "cost_including_validation": cold_cost,
            },
            "held_out_reaching": {
                "status": "passed",
                "target_count": len(held_out),
                "success_count": sum(item["status"] == "passed" for item in held_out),
                "targets": held_out,
            },
            "warm_reuse": {
                "candidate_source": "cold_probe_acquired_mapping",
                "fresh_validation_probe": warm_probe,
                "validation": warm_validation,
                "validation_cost": warm_validation_cost,
                "held_out_target": warm_trial,
                "agent_execution": agent_execution,
            },
            "refusals": refusals,
            "safety_bounds": {
                "maximum_action_delta_radians": MAX_ACTION_DELTA_RADIANS,
                "local_request_limit_radians": LOCAL_REQUEST_LIMIT_RADIANS,
                "target_tolerance_px": TARGET_TOLERANCE_PX,
            },
            "trace": {
                "path": "trace.jsonl",
                "action_count": len(trace),
                "generated_request_ids": "local UUID4; no model supplied identifiers",
            },
            "timing_ms": {"total": _round((time.monotonic() - started) * 1000)},
            "claim_boundary": (
                "All features, joint effects, and action outcomes come from public ROS "
                "interfaces. The learned mapping is local and one dimensional; no "
                "simulator internal state or global reachability is claimed."
            ),
        }
    finally:
        node.destroy_node()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    trace: list[dict[str, object]] = []

    rclpy.init()
    try:
        try:
            payload = qualify(args.output.parent, trace)
        except Exception as error:  # preserve failure evidence at the process boundary
            payload = {
                "schema_version": 1,
                "status": "failed",
                "recorded_at": datetime.now(UTC).isoformat(),
                "error_type": type(error).__name__,
                "error": str(error),
                "trace_action_count": len(trace),
                "claim_boundary": "No visual reaching capability is claimed.",
            }
            _write_trace(args.output.parent / "trace.jsonl", trace)
            _write_create_only(args.output, payload)
            print(f"Harbor reach failed: {error}")
            return 1
        _write_trace(args.output.parent / "trace.jsonl", trace)
        _write_create_only(args.output, payload)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    finally:
        rclpy.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
