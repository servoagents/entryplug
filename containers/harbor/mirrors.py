#!/usr/bin/python3
"""Qualify active association across two rendered cameras and a delayed path."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import random
import statistics
import time
from collections.abc import Mapping
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path

import cv2
import numpy as np
import rclpy
from rclpy.qos import qos_profile_sensor_data
from reach import (
    Frame,
    Observer,
    _establish_visibility_anchor,
    _marker,
    _measure_probe,
    _no_action_noise,
    _return_to_anchor,
    _round,
    _spin_until,
    _write_create_only,
    _write_png_create_only,
    _write_trace,
)
from sensor_msgs.msg import Image

from entryplug.agent import AgentRunner, ScriptedExplorer, agent_execution_record
from entryplug.association import (
    AssociationProfile,
    CandidateEvidence,
    load_visual_binding,
    make_visual_binding_record,
    metadata_only_selection,
    select_candidate,
    validate_cached_binding,
)
from entryplug.evidence import EvidenceQuery, JsonValue, SqliteEvidenceCache
from entryplug.operation import (
    CapabilitySpec,
    Lifecycle,
    MotionState,
    OperationContext,
    OperationHost,
    OperationResult,
)
from entryplug.session import Session

SOLVER_SEED = 947
SOURCE_NAME_SEED = 7043
DELAY_SECONDS = 0.36
FIT_PROBES_RADIANS = (0.018, -0.031, 0.047, -0.022, 0.036, -0.044)
VALIDATION_PROBES_RADIANS = (0.028, -0.049, 0.041, -0.034)
REUSE_PROBES_RADIANS = (-0.027, 0.023)
MINIMUM_PROBE_GAP_SECONDS = 0.12
MAXIMUM_PROBE_GAP_SECONDS = 0.62
BINDING_CONTEXT = {
    "task": "visual_reach",
    "model_version": "scalar-jacobian-v1",
    "fixture": "harbor-mirrors-v1",
    "source_convention": "red-centroid-y-v1",
}


class MirrorsObserver(Observer):
    """Observe the controlled and independently driven rendered cameras."""

    def __init__(self) -> None:
        super().__init__("entryplug_hall_of_mirrors")
        self.mirror_frame: Frame | None = None
        self.mirror_frames: list[Frame] = []
        self.create_subscription(
            Image,
            "/camera/mirror/image_raw",
            self._mirror_image,
            qos_profile_sensor_data,
        )

    def _mirror_image(self, message: Image) -> None:
        data = bytes(message.data)
        frame = Frame(
            sequence=(self.mirror_frame.sequence + 1) if self.mirror_frame else 1,
            received_monotonic=time.monotonic(),
            width=message.width,
            height=message.height,
            encoding=message.encoding,
            step=message.step,
            stamp_sec=message.header.stamp.sec,
            stamp_nanosec=message.header.stamp.nanosec,
            sha256=hashlib.sha256(data).hexdigest(),
            data=data,
        )
        self.mirror_frame = frame
        self.mirror_frames.append(frame)
        if len(self.mirror_frames) > 256:
            del self.mirror_frames[:128]


def _opaque_ids() -> tuple[list[str], list[str]]:
    generator = random.Random(SOURCE_NAME_SEED)
    candidate_ids = [f"view-{generator.getrandbits(32):08x}" for _ in range(4)]
    lineage_ids = [f"lineage-{generator.getrandbits(48):012x}" for _ in range(3)]
    return candidate_ids, lineage_ids


def _fresh_mirror_observation(
    node: MirrorsObserver, *, after_sequence: int | None = None, samples: int = 3
) -> dict[str, object]:
    start_sequence = (
        after_sequence
        if after_sequence is not None
        else (node.mirror_frame.sequence if node.mirror_frame is not None else 0)
    )
    observations: list[dict[str, object]] = []
    frames: list[Frame] = []
    last_sequence = start_sequence
    deadline = time.monotonic() + 4.0
    while rclpy.ok() and time.monotonic() < deadline and len(observations) < samples:
        rclpy.spin_once(node, timeout_sec=0.1)
        frame = node.mirror_frame
        if frame is None or frame.sequence <= last_sequence:
            continue
        observations.append(_marker(frame))
        frames.append(frame)
        last_sequence = frame.sequence
    if len(observations) != samples:
        raise TimeoutError(f"received {len(observations)} of {samples} fresh mirror camera samples")
    x_values = [float(item["x_px"]) for item in observations]
    y_values = [float(item["y_px"]) for item in observations]
    last = observations[-1]
    return {
        "x_px": _round(statistics.median(x_values)),
        "y_px": _round(statistics.median(y_values)),
        "sample_count": samples,
        "x_range_px": _round(max(x_values) - min(x_values)),
        "y_range_px": _round(max(y_values) - min(y_values)),
        "first_frame_sequence": observations[0]["frame"]["sequence"],
        "last_frame_sequence": last["frame"]["sequence"],
        "last_frame": last["frame"],
        "last_receive_age_ms": _round(
            max(0.0, (time.monotonic() - frames[-1].received_monotonic) * 1000)
        ),
        "marker_area_px": last["area_px"],
        "bounding_box_px": last["bounding_box_px"],
    }


def _mirror_marker_is_visible(node: MirrorsObserver) -> bool:
    if node.mirror_frame is None:
        return False
    try:
        _marker(node.mirror_frame)
    except RuntimeError:
        return False
    return True


def _mirror_noise(node: MirrorsObserver, samples: int = 8) -> dict[str, object]:
    values = [_fresh_mirror_observation(node, samples=1) for _ in range(samples)]
    x_values = [float(item["x_px"]) for item in values]
    y_values = [float(item["y_px"]) for item in values]
    return {
        "commands_applied_by_entryplug": 0,
        "fixture_driver_active": True,
        "sample_count": samples,
        "x_range_px": _round(max(x_values) - min(x_values)),
        "y_range_px": _round(max(y_values) - min(y_values)),
        "y_population_stddev_px": _round(statistics.pstdev(y_values)),
        "first_frame_sequence": values[0]["last_frame_sequence"],
        "last_frame_sequence": values[-1]["last_frame_sequence"],
    }


def _wait_probe_gap(node: MirrorsObserver, seconds: float) -> float:
    started = time.monotonic()
    _spin_until(
        node,
        lambda: time.monotonic() - started >= seconds,
        seconds + 1.0,
        "randomized pre-probe gap",
    )
    return _round(time.monotonic() - started)


def _deliver_delayed_copy(
    node: MirrorsObserver,
    probe: dict[str, object],
    trace_item: dict[str, object],
) -> dict[str, object]:
    queued = time.monotonic()
    _spin_until(
        node,
        lambda: time.monotonic() - queued >= DELAY_SECONDS,
        DELAY_SECONDS + 1.0,
        "delayed observation path",
    )
    delivered = time.monotonic()
    return {
        "delay_ms": _round((delivered - queued) * 1000),
        "effect_px": probe["observed_feature_delta_px"],
        "source_request_id": probe["request_id"],
        "source_goal_id": probe["goal_id"],
        "source_before_frame": trace_item["before_feature"]["last_frame"],
        "source_after_frame": trace_item["after_feature"]["last_frame"],
        "capture_stamps_preserved": True,
    }


def _find_frame(frames: list[Frame], sequence: int, label: str) -> Frame:
    for frame in reversed(frames):
        if frame.sequence == sequence:
            return frame
    raise RuntimeError(f"{label} frame sequence {sequence} is no longer retained")


def _frame_rgb(frame: Frame) -> np.ndarray:
    if frame.encoding != "rgb8" or frame.step != frame.width * 3:
        raise RuntimeError(
            f"review images require packed rgb8, got {frame.encoding!r} with step {frame.step}"
        )
    return np.frombuffer(frame.data, dtype=np.uint8).reshape((frame.height, frame.width, 3)).copy()


def _annotated(frame: Frame, feature: dict[str, object], label: str) -> np.ndarray:
    rgb = _frame_rgb(frame)
    box = feature["bounding_box_px"]
    left, top = int(box["left"]), int(box["top"])
    right, bottom = int(box["right"]), int(box["bottom"])
    center = (int(round(float(feature["x_px"]))), int(round(float(feature["y_px"]))))
    cv2.rectangle(rgb, (left, top), (right, bottom), (255, 255, 0), 2)
    cv2.drawMarker(rgb, center, (0, 255, 0), cv2.MARKER_CROSS, 18, 2)
    cv2.rectangle(rgb, (0, 0), (frame.width, 34), (0, 0, 0), -1)
    cv2.putText(
        rgb,
        label,
        (12, 24),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.65,
        (255, 255, 255),
        2,
        cv2.LINE_AA,
    )
    return rgb


def _write_rgb_png_create_only(path: Path, rgb: np.ndarray) -> None:
    encoded, contents = cv2.imencode(".png", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
    if not encoded:
        raise RuntimeError("OpenCV could not encode the review image")
    with path.open("xb") as stream:
        stream.write(contents.tobytes())


def _write_review_pair(
    path: Path,
    before_frame: Frame,
    before_feature: dict[str, object],
    after_frame: Frame,
    after_feature: dict[str, object],
    labels: tuple[str, str],
) -> None:
    review = np.concatenate(
        (
            _annotated(before_frame, before_feature, labels[0]),
            _annotated(after_frame, after_feature, labels[1]),
        ),
        axis=1,
    )
    legend = (
        "BLUE = CAMERA FOREARM  |  RED = TRACKED UPPER ARM  |  "
        "YELLOW BOX + GREEN CROSS = MEASUREMENT"
    )
    height, width = review.shape[:2]
    cv2.rectangle(review, (0, height - 30), (width, height), (0, 0, 0), -1)
    cv2.putText(
        review,
        legend,
        (12, height - 9),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.55,
        (255, 255, 255),
        1,
        cv2.LINE_AA,
    )
    _write_rgb_png_create_only(path, review)


def _probe_trace_panel(
    records: list[dict[str, object]],
    controlled_id: str,
    independent_id: str,
    gain_px_per_radian: float,
) -> np.ndarray:
    width, height = 1280, 300
    panel = np.full((height, width, 3), (17, 24, 39), dtype=np.uint8)
    white = (240, 244, 248)
    muted = (148, 163, 184)
    expected_color = (56, 189, 248)
    controlled_color = (52, 211, 153)
    independent_color = (251, 146, 60)
    cv2.putText(
        panel,
        "OBSERVER ONLY: PROBE RESPONSE OVER WALL TIME",
        (24, 31),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.72,
        white,
        2,
        cv2.LINE_AA,
    )
    cv2.putText(
        panel,
        "ONE FRAME IS AMBIGUOUS; THE PROBE SEQUENCE DISAMBIGUATES",
        (665, 29),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.48,
        white,
        1,
        cv2.LINE_AA,
    )
    legend = "CYAN expected from fitted gain   GREEN controlled camera   ORANGE independent camera"
    cv2.putText(
        panel,
        legend,
        (24, 57),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.5,
        muted,
        1,
        cv2.LINE_AA,
    )

    commands = [float(record["command_radians"]) for record in records]
    expected = [gain_px_per_radian * command for command in commands]
    controlled = [float(record["candidate_effects_px"][controlled_id]) for record in records]
    independent = [float(record["candidate_effects_px"][independent_id]) for record in records]
    start_times = [float(record["probe_started_ms"]) for record in records]
    minimum_time, maximum_time = min(start_times), max(start_times)
    time_span = max(1.0, maximum_time - minimum_time)
    maximum_effect = max(
        1.0,
        *(abs(value) for value in expected + controlled + independent),
    )
    left, right = 70, width - 35
    top, bottom = 75, 235
    center_y = (top + bottom) // 2
    cv2.line(panel, (left, center_y), (right, center_y), (71, 85, 105), 1)
    cv2.putText(
        panel,
        "0 px",
        (15, center_y + 5),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.4,
        muted,
        1,
        cv2.LINE_AA,
    )

    def point(index: int, value: float) -> tuple[int, int]:
        x = left + int((start_times[index] - minimum_time) / time_span * (right - left))
        y = center_y - int(value / maximum_effect * (bottom - top) / 2)
        return x, y

    series = (
        (expected, expected_color),
        (controlled, controlled_color),
        (independent, independent_color),
    )
    for values, color in series:
        points = [point(index, value) for index, value in enumerate(values)]
        for first, second in zip(points, points[1:], strict=False):
            cv2.line(panel, first, second, color, 2, cv2.LINE_AA)
        for x, y in points:
            cv2.circle(panel, (x, y), 4, color, -1, cv2.LINE_AA)

    for index, record in enumerate(records):
        x, _ = point(index, 0.0)
        stage = {
            "fit_probe": "F",
            "validation_probe": "V",
            "reuse_probe": "R",
        }[str(record["event"])]
        label = f"{stage}{int(record['index'])} {commands[index]:+.3f}"
        cv2.putText(
            panel,
            label,
            (max(4, x - 32), 259),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.35,
            white,
            1,
            cv2.LINE_AA,
        )
        gap_ms = float(record["pre_probe_gap_ms"])
        cv2.putText(
            panel,
            f"gap {gap_ms:.0f}ms",
            (max(4, x - 32), 278),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.3,
            muted,
            1,
            cv2.LINE_AA,
        )
    return panel


def _write_agent_views_panel(
    path: Path,
    controlled_frame: Frame,
    controlled_feature: dict[str, object],
    independent_frame: Frame,
    independent_feature: dict[str, object],
) -> None:
    canvas = np.full((600, 400, 3), (8, 13, 24), dtype=np.uint8)
    controlled = cv2.resize(
        _annotated(controlled_frame, controlled_feature, "AGENT VIEW A"),
        (400, 300),
    )
    independent = cv2.resize(
        _annotated(independent_frame, independent_feature, "AGENT VIEW B"),
        (400, 300),
    )
    canvas[0:300, 0:400] = controlled
    canvas[300:600, 0:400] = independent
    _write_rgb_png_create_only(path, canvas)


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record, sort_keys=True))
            stream.write("\n")


def _candidate_record(evidence: CandidateEvidence) -> dict[str, object]:
    return asdict(evidence)


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


def _number_tuple(value: object, label: str) -> tuple[float, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{label} must be an array")
    return tuple(_number(item, label) for item in value)


def _candidate_from_record(value: object) -> CandidateEvidence:
    if not isinstance(value, Mapping):
        raise ValueError("each association candidate must be an object")
    expected = {
        "candidate_id",
        "lineage_id",
        "maximum_age_ms",
        "noise_range_px",
        "fit_commands_radians",
        "fit_effects_px",
        "validation_commands_radians",
        "validation_effects_px",
    }
    if set(value) != expected:
        raise ValueError("association candidate fields do not match the schema")
    candidate_id = value["candidate_id"]
    lineage_id = value["lineage_id"]
    if not isinstance(candidate_id, str) or not candidate_id:
        raise ValueError("candidate ID must be a nonempty string")
    if not isinstance(lineage_id, str) or not lineage_id:
        raise ValueError("lineage ID must be a nonempty string")
    return CandidateEvidence(
        candidate_id=candidate_id,
        lineage_id=lineage_id,
        maximum_age_ms=_number(value["maximum_age_ms"], "maximum age"),
        noise_range_px=_number(value["noise_range_px"], "noise range"),
        fit_commands_radians=_number_tuple(value["fit_commands_radians"], "fit commands"),
        fit_effects_px=_number_tuple(value["fit_effects_px"], "fit effects"),
        validation_commands_radians=_number_tuple(
            value["validation_commands_radians"], "validation commands"
        ),
        validation_effects_px=_number_tuple(value["validation_effects_px"], "validation effects"),
    )


def _association_arguments(arguments: Mapping[str, JsonValue]) -> Mapping[str, object]:
    if set(arguments) != {"candidates"}:
        raise ValueError("association requires exactly one candidate array")
    raw_candidates = arguments["candidates"]
    if not isinstance(raw_candidates, (list, tuple)) or not raw_candidates:
        raise ValueError("association requires at least one candidate")
    candidates = tuple(_candidate_from_record(item) for item in raw_candidates)
    return {"candidates": [_candidate_record(item) for item in candidates]}


async def _agent_association(
    candidates: list[CandidateEvidence],
    profile: AssociationProfile,
) -> tuple[dict[str, object], dict[str, object]]:
    """Select an opaque source through the shared agent and operation boundary."""

    selection_result: dict[str, object] = {}

    async def associate(
        context: OperationContext,
        arguments: Mapping[str, JsonValue],
    ) -> OperationResult:
        if context.cancel_requested:
            return OperationResult(
                Lifecycle.CANCELED,
                MotionState.IDLE,
                reason_code="CANCEL_REQUESTED",
            )
        context.report("associate", MotionState.IDLE)
        raw_candidates = arguments["candidates"]
        if not isinstance(raw_candidates, (list, tuple)):
            raise ValueError("validated candidates are unavailable")
        checked = tuple(_candidate_from_record(item) for item in raw_candidates)
        result = select_candidate(checked, profile)
        selection_result.update(result)
        return OperationResult(Lifecycle.SUCCEEDED, MotionState.IDLE, result=result)

    host = OperationHost(
        (
            CapabilitySpec(
                "associate_visual_sources",
                "1",
                "Select one usable opaque visual lineage from intervention evidence",
                False,
                5.0,
                0.25,
                _association_arguments,
                associate,
            ),
        )
    )
    session = Session(host, owns_runtime=True)
    runner = AgentRunner(
        ScriptedExplorer(
            "associate_visual_sources",
            {"candidates": [_candidate_record(item) for item in candidates]},
        ),
        decision_timeout_seconds=2.0,
    )
    try:
        steps = await runner.run_until_stop(session, maximum_decisions=8)
        view = await session.observe()
        if len(view.operations) != 1:
            raise RuntimeError("scripted association did not produce exactly one operation")
        operation = view.operations[0]
        if operation.lifecycle != Lifecycle.SUCCEEDED or not selection_result:
            raise RuntimeError(
                f"agent association finished as {operation.lifecycle.value}: "
                f"{operation.reason_code}"
            )
        execution = agent_execution_record(
            steps,
            operation,
            policy="deterministic_scripted",
        )
        execution["available_to_policy"] = {
            "candidate_roles": False,
            "spectator_camera": False,
            "private_motion_schedule": False,
            "opaque_candidate_evidence": True,
        }
        return selection_result, execution
    finally:
        await runner.close()
        await session.close()


def qualify(
    output_dir: Path,
    trace: list[dict[str, object]],
    public_events: list[dict[str, object]],
) -> tuple[dict[str, object], dict[str, object]]:
    started = time.monotonic()
    node = MirrorsObserver()
    try:
        _spin_until(
            node,
            lambda: (
                node.frame is not None and node.mirror_frame is not None and node.joints is not None
            ),
            30.0,
            "two candidate cameras and joint topics",
        )
        if not node.action.wait_for_server(timeout_sec=15.0):
            raise TimeoutError("trajectory action server was unavailable")
        assert node.frame is not None
        controlled_at_rest = node.frame
        _write_png_create_only(output_dir / "controlled-at-rest.raw.png", controlled_at_rest)
        anchor, visibility_anchor = _establish_visibility_anchor(node, trace)
        controlled_anchor_feature = visibility_anchor["after_feature"]
        controlled_anchor = _find_frame(
            node.frames,
            int(controlled_anchor_feature["last_frame_sequence"]),
            "controlled anchor",
        )
        _write_png_create_only(output_dir / "controlled-anchor.raw.png", controlled_anchor)
        direct_noise = _no_action_noise(node)

        _spin_until(
            node,
            lambda: _mirror_marker_is_visible(node),
            10.0,
            "visible red marker in the independent camera",
        )
        mirror_preview_feature = _fresh_mirror_observation(node)
        mirror_preview = _find_frame(
            node.mirror_frames,
            int(mirror_preview_feature["last_frame_sequence"]),
            "mirror preview",
        )
        _write_png_create_only(output_dir / "independent-camera-preview.raw.png", mirror_preview)
        independent_noise = _mirror_noise(node)

        candidate_ids, lineage_ids = _opaque_ids()
        controlled_id, delayed_id, independent_id, ambiguous_id = candidate_ids
        controlled_lineage, independent_lineage, ambiguous_lineage = lineage_ids
        catalog = [
            {"candidate_id": controlled_id, "lineage_id": controlled_lineage},
            {"candidate_id": delayed_id, "lineage_id": controlled_lineage},
            {"candidate_id": independent_id, "lineage_id": independent_lineage},
        ]
        random.Random(SOURCE_NAME_SEED + 1).shuffle(catalog)
        public_events.append(
            {
                "event": "candidate_catalog",
                "candidate_order": catalog,
                "role_labels_available": False,
            }
        )

        solver = random.Random(SOLVER_SEED)
        fit_commands = list(FIT_PROBES_RADIANS)
        validation_commands = list(VALIDATION_PROBES_RADIANS)
        solver.shuffle(fit_commands)
        solver.shuffle(validation_commands)

        direct_fit: list[float] = []
        independent_fit: list[float] = []
        independent_ages: list[float] = []
        delayed_fit: list[dict[str, object]] = []
        direct_ages: list[float] = []
        fit_records: list[dict[str, object]] = []
        independent_pairs: list[dict[str, object]] = []
        direct_pairs: list[dict[str, object]] = []
        acquisition_started = time.monotonic()
        for index, command in enumerate(fit_commands):
            requested_gap = solver.uniform(MINIMUM_PROBE_GAP_SECONDS, MAXIMUM_PROBE_GAP_SECONDS)
            actual_gap = _wait_probe_gap(node, requested_gap)
            probe_started_ms = _round((time.monotonic() - started) * 1000)
            independent_before = _fresh_mirror_observation(node)
            probe = _measure_probe(
                node,
                anchor,
                command,
                purpose=f"mirrors-fit-{index + 1}",
                trace=trace,
            )
            independent_after = _fresh_mirror_observation(
                node, after_sequence=int(independent_before["last_frame_sequence"])
            )
            direct_effect = float(probe["observed_feature_delta_px"])
            independent_effect = _round(
                float(independent_after["y_px"]) - float(independent_before["y_px"])
            )
            trace_item = next(
                item for item in trace if item.get("request_id") == probe["request_id"]
            )
            direct_pairs.append(
                {
                    "effect_px": direct_effect,
                    "before_feature": trace_item["before_feature"],
                    "after_feature": trace_item["after_feature"],
                    "before_frame": _find_frame(
                        node.frames,
                        int(trace_item["before_feature"]["last_frame_sequence"]),
                        "controlled fit probe before",
                    ),
                    "after_frame": _find_frame(
                        node.frames,
                        int(trace_item["after_feature"]["last_frame_sequence"]),
                        "controlled fit probe after",
                    ),
                }
            )
            direct_ages.append(float(trace_item["after_feature"]["last_receive_age_ms"]))
            independent_ages.append(
                max(
                    float(independent_before["last_receive_age_ms"]),
                    float(independent_after["last_receive_age_ms"]),
                )
            )
            delayed = _deliver_delayed_copy(node, probe, trace_item)
            direct_fit.append(direct_effect)
            independent_fit.append(independent_effect)
            delayed_fit.append(delayed)
            independent_pairs.append(
                {
                    "effect_px": independent_effect,
                    "before": independent_before,
                    "after": independent_after,
                    "before_frame": _find_frame(
                        node.mirror_frames,
                        int(independent_before["last_frame_sequence"]),
                        "independent fit interval before",
                    ),
                    "after_frame": _find_frame(
                        node.mirror_frames,
                        int(independent_after["last_frame_sequence"]),
                        "independent fit interval after",
                    ),
                }
            )
            record = {
                "event": "fit_probe",
                "index": index + 1,
                "command_radians": command,
                "pre_probe_gap_ms": _round(actual_gap * 1000),
                "probe_started_ms": probe_started_ms,
                "probe_completed_ms": _round((time.monotonic() - started) * 1000),
                "request_id": probe["request_id"],
                "goal_id": probe["goal_id"],
                "candidate_effects_px": {
                    controlled_id: direct_effect,
                    delayed_id: delayed["effect_px"],
                    independent_id: independent_effect,
                },
                "independent_sample_ids": {
                    "before": independent_before["last_frame_sequence"],
                    "after": independent_after["last_frame_sequence"],
                },
            }
            fit_records.append(record)
            public_events.append(record)

        direct_validation: list[float] = []
        independent_validation: list[float] = []
        delayed_validation: list[dict[str, object]] = []
        validation_records: list[dict[str, object]] = []
        for index, command in enumerate(validation_commands):
            requested_gap = solver.uniform(MINIMUM_PROBE_GAP_SECONDS, MAXIMUM_PROBE_GAP_SECONDS)
            actual_gap = _wait_probe_gap(node, requested_gap)
            probe_started_ms = _round((time.monotonic() - started) * 1000)
            independent_before = _fresh_mirror_observation(node)
            probe = _measure_probe(
                node,
                anchor,
                command,
                purpose=f"mirrors-validation-{index + 1}",
                trace=trace,
            )
            independent_after = _fresh_mirror_observation(
                node, after_sequence=int(independent_before["last_frame_sequence"])
            )
            direct_effect = float(probe["observed_feature_delta_px"])
            independent_effect = _round(
                float(independent_after["y_px"]) - float(independent_before["y_px"])
            )
            trace_item = next(
                item for item in trace if item.get("request_id") == probe["request_id"]
            )
            direct_pairs.append(
                {
                    "effect_px": direct_effect,
                    "before_feature": trace_item["before_feature"],
                    "after_feature": trace_item["after_feature"],
                    "before_frame": _find_frame(
                        node.frames,
                        int(trace_item["before_feature"]["last_frame_sequence"]),
                        "controlled validation probe before",
                    ),
                    "after_frame": _find_frame(
                        node.frames,
                        int(trace_item["after_feature"]["last_frame_sequence"]),
                        "controlled validation probe after",
                    ),
                }
            )
            direct_ages.append(float(trace_item["after_feature"]["last_receive_age_ms"]))
            independent_ages.append(
                max(
                    float(independent_before["last_receive_age_ms"]),
                    float(independent_after["last_receive_age_ms"]),
                )
            )
            delayed = _deliver_delayed_copy(node, probe, trace_item)
            direct_validation.append(direct_effect)
            independent_validation.append(independent_effect)
            delayed_validation.append(delayed)
            independent_pairs.append(
                {
                    "effect_px": independent_effect,
                    "before": independent_before,
                    "after": independent_after,
                    "before_frame": _find_frame(
                        node.mirror_frames,
                        int(independent_before["last_frame_sequence"]),
                        "independent validation interval before",
                    ),
                    "after_frame": _find_frame(
                        node.mirror_frames,
                        int(independent_after["last_frame_sequence"]),
                        "independent validation interval after",
                    ),
                }
            )
            record = {
                "event": "validation_probe",
                "index": index + 1,
                "command_radians": command,
                "pre_probe_gap_ms": _round(actual_gap * 1000),
                "probe_started_ms": probe_started_ms,
                "probe_completed_ms": _round((time.monotonic() - started) * 1000),
                "request_id": probe["request_id"],
                "goal_id": probe["goal_id"],
                "candidate_effects_px": {
                    controlled_id: direct_effect,
                    delayed_id: delayed["effect_px"],
                    independent_id: independent_effect,
                },
                "independent_sample_ids": {
                    "before": independent_before["last_frame_sequence"],
                    "after": independent_after["last_frame_sequence"],
                },
            }
            validation_records.append(record)
            public_events.append(record)

        maximum_delay_ms = max(float(item["delay_ms"]) for item in delayed_fit + delayed_validation)
        controlled = CandidateEvidence(
            candidate_id=controlled_id,
            lineage_id=controlled_lineage,
            maximum_age_ms=max(direct_ages),
            noise_range_px=float(direct_noise["y_range_px"]),
            fit_commands_radians=tuple(fit_commands),
            fit_effects_px=tuple(direct_fit),
            validation_commands_radians=tuple(validation_commands),
            validation_effects_px=tuple(direct_validation),
        )
        delayed = replace(
            controlled,
            candidate_id=delayed_id,
            maximum_age_ms=maximum_delay_ms,
        )
        independent = CandidateEvidence(
            candidate_id=independent_id,
            lineage_id=independent_lineage,
            maximum_age_ms=max(independent_ages),
            noise_range_px=float(independent_noise["y_range_px"]),
            fit_commands_radians=tuple(fit_commands),
            fit_effects_px=tuple(independent_fit),
            validation_commands_radians=tuple(validation_commands),
            validation_effects_px=tuple(independent_validation),
        )
        candidates = [controlled, delayed, independent]
        random.Random(SOURCE_NAME_SEED + 2).shuffle(candidates)
        profile = AssociationProfile()
        metadata_baseline = metadata_only_selection(candidates, profile)
        selection, agent_execution = asyncio.run(_agent_association(candidates, profile))
        acquisition_duration_ms = _round((time.monotonic() - acquisition_started) * 1000)

        ambiguous = replace(
            controlled,
            candidate_id=ambiguous_id,
            lineage_id=ambiguous_lineage,
        )
        ambiguity_check = select_candidate((controlled, ambiguous), profile)
        public_events.append(
            {
                "event": "association_result",
                "selection": selection,
                "agent_execution": agent_execution,
                "metadata_only_baseline": metadata_baseline,
                "ambiguity_check": ambiguity_check,
            }
        )

        controlled_assessment = next(
            item for item in selection["assessments"] if item["candidate_id"] == controlled_id
        )
        binding_record = make_visual_binding_record(
            context=BINDING_CONTEXT,
            candidate_id=controlled_id,
            lineage_id=controlled_lineage,
            gain_px_per_radian=float(controlled_assessment["gain_px_per_radian"]),
            validity_radians=(
                min(FIT_PROBES_RADIANS + VALIDATION_PROBES_RADIANS),
                max(FIT_PROBES_RADIANS + VALIDATION_PROBES_RADIANS),
            ),
            noise_range_px=float(direct_noise["y_range_px"]),
            maximum_age_ms=profile.maximum_age_ms,
            timing_method_id="settled-before-after-v1",
            evidence_refs=("mirrors.json", "trace.jsonl"),
            created_at=datetime.now(UTC).isoformat(),
        )
        cache_path = output_dir / "evidence-cache.private.sqlite3"
        cache = SqliteEvidenceCache(cache_path)
        try:
            stored_key = cache.store(binding_record)
            cached_candidates = cache.find(
                EvidenceQuery(kind=binding_record.kind, context=BINDING_CONTEXT),
                limit=1,
            )
            if not cached_candidates:
                raise RuntimeError("stored visual binding was not retrievable")
            loaded_record = cache.load(cached_candidates[0].key)
            if loaded_record is None or loaded_record.key != stored_key:
                raise RuntimeError("loaded visual binding did not match stored evidence")
            cached_binding = load_visual_binding(loaded_record)
        finally:
            cache.close()

        reuse_started = time.monotonic()
        reuse_commands = list(REUSE_PROBES_RADIANS)
        solver.shuffle(reuse_commands)
        reuse_direct: list[float] = []
        reuse_independent: list[float] = []
        reuse_delayed: list[dict[str, object]] = []
        reuse_direct_ages: list[float] = []
        reuse_independent_ages: list[float] = []
        reuse_records: list[dict[str, object]] = []
        for index, command in enumerate(reuse_commands):
            requested_gap = solver.uniform(MINIMUM_PROBE_GAP_SECONDS, MAXIMUM_PROBE_GAP_SECONDS)
            actual_gap = _wait_probe_gap(node, requested_gap)
            probe_started_ms = _round((time.monotonic() - started) * 1000)
            independent_before = _fresh_mirror_observation(node)
            probe = _measure_probe(
                node,
                anchor,
                command,
                purpose=f"mirrors-reuse-{index + 1}",
                trace=trace,
            )
            independent_after = _fresh_mirror_observation(
                node, after_sequence=int(independent_before["last_frame_sequence"])
            )
            direct_effect = float(probe["observed_feature_delta_px"])
            independent_effect = _round(
                float(independent_after["y_px"]) - float(independent_before["y_px"])
            )
            trace_item = next(
                item for item in trace if item.get("request_id") == probe["request_id"]
            )
            direct_pairs.append(
                {
                    "effect_px": direct_effect,
                    "before_feature": trace_item["before_feature"],
                    "after_feature": trace_item["after_feature"],
                    "before_frame": _find_frame(
                        node.frames,
                        int(trace_item["before_feature"]["last_frame_sequence"]),
                        "controlled reuse probe before",
                    ),
                    "after_frame": _find_frame(
                        node.frames,
                        int(trace_item["after_feature"]["last_frame_sequence"]),
                        "controlled reuse probe after",
                    ),
                }
            )
            independent_pairs.append(
                {
                    "effect_px": independent_effect,
                    "before": independent_before,
                    "after": independent_after,
                    "before_frame": _find_frame(
                        node.mirror_frames,
                        int(independent_before["last_frame_sequence"]),
                        "independent reuse interval before",
                    ),
                    "after_frame": _find_frame(
                        node.mirror_frames,
                        int(independent_after["last_frame_sequence"]),
                        "independent reuse interval after",
                    ),
                }
            )
            reuse_direct.append(direct_effect)
            reuse_independent.append(independent_effect)
            reuse_direct_ages.append(float(trace_item["after_feature"]["last_receive_age_ms"]))
            reuse_independent_ages.append(
                max(
                    float(independent_before["last_receive_age_ms"]),
                    float(independent_after["last_receive_age_ms"]),
                )
            )
            delayed_copy = _deliver_delayed_copy(node, probe, trace_item)
            reuse_delayed.append(delayed_copy)
            record = {
                "event": "reuse_probe",
                "index": index + 1,
                "command_radians": command,
                "pre_probe_gap_ms": _round(actual_gap * 1000),
                "probe_started_ms": probe_started_ms,
                "probe_completed_ms": _round((time.monotonic() - started) * 1000),
                "request_id": probe["request_id"],
                "goal_id": probe["goal_id"],
                "candidate_effects_px": {
                    controlled_id: direct_effect,
                    delayed_id: delayed_copy["effect_px"],
                    independent_id: independent_effect,
                },
                "independent_sample_ids": {
                    "before": independent_before["last_frame_sequence"],
                    "after": independent_after["last_frame_sequence"],
                },
            }
            reuse_records.append(record)
            public_events.append(record)

        checked_reuse = validate_cached_binding(
            cached_binding,
            commands_radians=reuse_commands,
            candidate_effects_px={
                controlled_id: reuse_direct,
                delayed_id: reuse_direct,
                independent_id: reuse_independent,
            },
            candidate_lineages={
                controlled_id: controlled_lineage,
                delayed_id: controlled_lineage,
                independent_id: independent_lineage,
            },
            candidate_maximum_ages_ms={
                controlled_id: max(reuse_direct_ages),
                delayed_id: max(float(item["delay_ms"]) for item in reuse_delayed),
                independent_id: max(reuse_independent_ages),
            },
            candidate_noise_ranges_px={
                controlled_id: float(direct_noise["y_range_px"]),
                delayed_id: float(direct_noise["y_range_px"]),
                independent_id: float(independent_noise["y_range_px"]),
            },
            profile=profile,
        )
        reuse_duration_ms = _round((time.monotonic() - reuse_started) * 1000)
        reuse_work = {
            "scope": "warm reuse within one live fixture episode",
            "cache_backend": "sqlite",
            "cache_hit_granted_readiness": False,
            "evidence_key": binding_record.key,
            "validation": checked_reuse,
            "full_reacquisition": {
                "probe_count": len(fit_commands) + len(validation_commands),
                "command_travel_radians": _round(
                    sum(abs(value) for value in fit_commands + validation_commands)
                ),
                "duration_ms": acquisition_duration_ms,
            },
            "checked_reuse": {
                "probe_count": len(reuse_commands),
                "command_travel_radians": _round(sum(abs(value) for value in reuse_commands)),
                "duration_ms": reuse_duration_ms,
            },
        }
        public_events.append(
            {
                "event": "checked_reuse_result",
                "evidence_key": binding_record.key,
                "result": checked_reuse,
            }
        )

        representative_direct = max(direct_pairs, key=lambda item: abs(float(item["effect_px"])))
        direct_before_feature = representative_direct["before_feature"]
        direct_after_feature = representative_direct["after_feature"]
        direct_before_frame = representative_direct["before_frame"]
        direct_after_frame = representative_direct["after_frame"]
        representative_independent = max(
            independent_pairs, key=lambda item: abs(float(item["effect_px"]))
        )
        independent_before_feature = representative_independent["before"]
        independent_after_feature = representative_independent["after"]
        independent_before_frame = representative_independent["before_frame"]
        independent_after_frame = representative_independent["after_frame"]
        raw_frames = {
            "controlled-probe-before.raw.png": direct_before_frame,
            "controlled-probe-after.raw.png": direct_after_frame,
            "independent-probe-before.raw.png": independent_before_frame,
            "independent-probe-after.raw.png": independent_after_frame,
        }
        for name, frame in raw_frames.items():
            _write_png_create_only(output_dir / name, frame)
        controlled_delta = float(representative_direct["effect_px"])
        _write_review_pair(
            output_dir / "review-controlled-probe.png",
            direct_before_frame,
            direct_before_feature,
            direct_after_frame,
            direct_after_feature,
            (
                "CONTROLLED CAMERA: BEFORE",
                f"AFTER PROBE: RED CENTROID DY {controlled_delta:+.1f} PX",
            ),
        )
        independent_delta = float(representative_independent["effect_px"])
        _write_review_pair(
            output_dir / "review-independent-probe.png",
            independent_before_frame,
            independent_before_feature,
            independent_after_frame,
            independent_after_feature,
            (
                "INDEPENDENT CAMERA: BEFORE",
                f"LATER: RED CENTROID DY {independent_delta:+.1f} PX",
            ),
        )
        all_probe_records = fit_records + validation_records + reuse_records
        trace_panel = _probe_trace_panel(
            all_probe_records,
            controlled_id,
            independent_id,
            float(controlled_assessment["gain_px_per_radian"]),
        )
        _write_rgb_png_create_only(output_dir / "probe-response-trace.observer.png", trace_panel)
        _write_agent_views_panel(
            output_dir / "agent-views.observer.png",
            controlled_anchor,
            controlled_anchor_feature,
            mirror_preview,
            mirror_preview_feature,
        )

        image_manifest = {
            "schema_version": 2,
            "raw_files_are_unmodified_sensor_captures": True,
            "information_boundary": {
                "association_inputs": [
                    "/camera/color/image_raw",
                    "/camera/mirror/image_raw",
                ],
                "observer_only_topic": "/camera/spectator/image_raw",
                "observer_capture_process": "spectator_capture.py",
                "observer_report_process": "mirrors_report.py",
                "observer_topic_subscribed_by_agent_process": False,
                "observer_images_used_by_solver": False,
                "role_labels_used_by_solver": False,
            },
            "visual_key": {
                "blue_shape": "forearm carrying the camera, seen in perspective",
                "red_shape": "upper arm exposed behind the blue forearm",
                "dark_pixels": (
                    "self occlusion, lighting, or near-camera clipping; they are not "
                    "the tracked feature"
                ),
                "tracked_feature": "centroid and bounding box of the red pixels",
                "review_overlay": "yellow bounding box and green centroid cross",
            },
            "files": {
                "controlled-at-rest.raw.png": {
                    "kind": "raw",
                    "meaning": "controlled camera before the visibility setup motion",
                    "expected": "red upper arm is almost completely occluded",
                },
                "controlled-anchor.raw.png": {
                    "kind": "raw",
                    "meaning": ("controlled camera at the visible local operating anchor"),
                    "expected": ("a measurable red patch appears beyond the blue forearm"),
                },
                "controlled-probe-before.raw.png": {
                    "kind": "raw",
                    "meaning": ("controlled camera immediately before a representative probe"),
                },
                "controlled-probe-after.raw.png": {
                    "kind": "raw",
                    "meaning": "controlled camera immediately after that probe settled",
                },
                "independent-camera-preview.raw.png": {
                    "kind": "raw",
                    "meaning": ("second MuJoCo camera on the independently driven mechanism"),
                },
                "independent-probe-before.raw.png": {
                    "kind": "raw",
                    "meaning": "independent camera before the representative interval",
                },
                "independent-probe-after.raw.png": {
                    "kind": "raw",
                    "meaning": "independent camera later in the same interval",
                },
                "review-controlled-probe.png": {
                    "kind": "annotated_review",
                    "sources": [
                        "controlled-probe-before.raw.png",
                        "controlled-probe-after.raw.png",
                    ],
                },
                "review-independent-probe.png": {
                    "kind": "annotated_review",
                    "sources": [
                        "independent-probe-before.raw.png",
                        "independent-probe-after.raw.png",
                    ],
                },
                "agent-views.observer.png": {
                    "kind": "observer_only",
                    "meaning": "two candidate views annotated after association",
                },
                "spectator-overview.observer.png": {
                    "kind": "observer_only",
                    "meaning": (
                        "external view with derived articulation markers and truth "
                        "labels, withheld from association"
                    ),
                },
                "spectator-overview.raw.png": {
                    "kind": "observer_only_raw",
                    "meaning": "unmodified external camera capture",
                },
                "probe-response-trace.observer.png": {
                    "kind": "observer_only",
                    "meaning": (
                        "post-evaluation comparison of expected, controlled, and "
                        "independent responses over the randomized probe sequence"
                    ),
                },
                "hall-of-mirrors-demo.observer.png": {
                    "kind": "observer_only_composite",
                    "meaning": ("spectator overview, two candidate views, and response trace"),
                },
                "observer-report.json": {
                    "kind": "evaluator_record",
                    "meaning": ("hashes report inputs and records the process boundary"),
                },
            },
        }
        _write_create_only(output_dir / "images.json", image_manifest)

        _return_to_anchor(node, anchor, purpose="mirrors:final-anchor", trace=trace)
        role_map = {
            controlled_id: "controlled_rendered_camera",
            delayed_id: "delayed_controlled_lineage",
            independent_id: "independent_rendered_camera",
            ambiguous_id: "negative_test_indistinguishable_clone",
        }
        assessments = {item["candidate_id"]: item for item in selection["assessments"]}
        gates = {
            "controlled_source_selected": (selection.get("candidate_id") == controlled_id),
            "metadata_alone_refused": metadata_baseline["status"] == "refused",
            "delayed_path_rejected_as_stale": "stale" in assessments[delayed_id]["reasons"],
            "delayed_path_preserved_lineage": (delayed.lineage_id == controlled.lineage_id),
            "independent_source_rejected": not assessments[independent_id]["eligible"],
            "ambiguous_sources_refused": ambiguity_check.get("reason_code") == "ambiguous_sources",
            "two_rendered_camera_topics_observed": (
                node.frame is not None and node.mirror_frame is not None
            ),
            "observer_camera_withheld_from_selection": (
                "/camera/spectator/image_raw"
                not in image_manifest["information_boundary"]["association_inputs"]
            ),
            "checked_reuse_accepted": checked_reuse["status"] == "reused",
            "checked_reuse_reduced_probe_count": (
                reuse_work["checked_reuse"]["probe_count"]
                < reuse_work["full_reacquisition"]["probe_count"]
            ),
            "checked_reuse_reduced_command_travel": (
                reuse_work["checked_reuse"]["command_travel_radians"]
                < reuse_work["full_reacquisition"]["command_travel_radians"]
            ),
            "checked_reuse_reduced_setup_time": (
                reuse_work["checked_reuse"]["duration_ms"]
                < reuse_work["full_reacquisition"]["duration_ms"]
            ),
            "isolated_agent_selected_source": (
                agent_execution["status"] == "passed"
                and agent_execution["separate_policy_process"] is True
                and agent_execution["operation"]["lifecycle"] == "succeeded"
            ),
        }
        if not all(gates.values()):
            failed = [name for name, passed in gates.items() if not passed]
            raise RuntimeError(f"association gates failed: {', '.join(failed)}")

        evaluation = {
            "schema_version": 1,
            "recorded_at": datetime.now(UTC).isoformat(),
            "private_source_roles": role_map,
            "gates": gates,
            "selected_role": role_map[str(selection["candidate_id"])],
            "truth_boundary": (
                "Source roles, the spectator camera, and the mirror driver schedule "
                "are fixture evaluation data; they were not inputs to candidate "
                "assessment or selection. The association process does not subscribe "
                "to the spectator topic."
            ),
            "private_fixture_schedule": "mirror-schedule.private.jsonl",
            "reuse_scope": "warm reuse within one live fixture episode",
        }
        summary = {
            "schema_version": 2,
            "status": "passed",
            "recorded_at": datetime.now(UTC).isoformat(),
            "seeds": {
                "solver": SOLVER_SEED,
                "source_names": SOURCE_NAME_SEED,
                "independent": True,
            },
            "interfaces": {
                "controlled_camera_topic": "/camera/color/image_raw",
                "independent_camera_topic": "/camera/mirror/image_raw",
                "observer_only_camera_topic": "/camera/spectator/image_raw",
                "joint_topic": "/joint_states",
                "trajectory_action": "/trajectory_controller/follow_joint_trajectory",
            },
            "candidate_order": [item.candidate_id for item in candidates],
            "candidate_evidence": [_candidate_record(item) for item in candidates],
            "association": selection,
            "agent_execution": agent_execution,
            "metadata_only_baseline": metadata_baseline,
            "ambiguity_negative_test": ambiguity_check,
            "visibility_anchor": visibility_anchor,
            "no_action_noise": {
                "controlled_path": direct_noise,
                "independent_path": independent_noise,
            },
            "delayed_path": {
                "configured_delay_ms": DELAY_SECONDS * 1000,
                "maximum_measured_delivery_ms": _round(maximum_delay_ms),
                "capture_stamps_preserved": all(
                    bool(item["capture_stamps_preserved"])
                    for item in delayed_fit + delayed_validation + reuse_delayed
                ),
                "same_lineage_as_controlled": True,
                "treated_as_independent_evidence": False,
            },
            "probe_count": len(fit_commands),
            "validation_probe_count": len(validation_commands),
            "reuse_probe_count": len(reuse_commands),
            "checked_reuse": reuse_work,
            "probe_timing": {
                "randomized_pre_probe_gaps": True,
                "minimum_requested_gap_ms": MINIMUM_PROBE_GAP_SECONDS * 1000,
                "maximum_requested_gap_ms": MAXIMUM_PROBE_GAP_SECONDS * 1000,
                "actual_gap_ms": [record["pre_probe_gap_ms"] for record in all_probe_records],
                "fixture_motion_schedule_available_to_solver": False,
                "fixture_motion_schedule_periodic": False,
            },
            "fit_probes": fit_records,
            "validation_probes": validation_records,
            "safety": {
                "maximum_probe_radians": max(
                    abs(value) for value in fit_commands + validation_commands + reuse_commands
                ),
                "all_native_actions_succeeded": all(
                    int(item["native_status"]) == 4 for item in trace
                ),
                "action_count": len(trace),
            },
            "artifacts": {
                "image_manifest": "images.json",
                "observer_demo": "hall-of-mirrors-demo.observer.png",
                "observer_agent_views": "agent-views.observer.png",
                "observer_probe_trace": "probe-response-trace.observer.png",
                "observer_overview": "spectator-overview.observer.png",
                "observer_report": "observer-report.json",
                "private_fixture_schedule": "mirror-schedule.private.jsonl",
                "private_evidence_cache": "evidence-cache.private.sqlite3",
                "public_events": "public.jsonl",
                "private_evaluation": "evaluation.jsonl",
                "action_trace": "trace.jsonl",
            },
            "timing_ms": {"total": _round((time.monotonic() - started) * 1000)},
            "claim_boundary": (
                "Both candidate sources are real MuJoCo camera renders. The second "
                "mechanism follows a hidden nonperiodic fixture schedule, while probe "
                "order and timing are randomized independently. The external overview "
                "is captured after association by an evaluator-only process. This "
                "qualifies local source association and checked within-episode reuse "
                "in this Harbor fixture. It does not yet qualify cross-episode reuse, "
                "invalidation recovery, or general causal identification."
            ),
        }
        return summary, evaluation
    finally:
        node.destroy_node()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    trace: list[dict[str, object]] = []
    public_events: list[dict[str, object]] = []

    rclpy.init()
    try:
        try:
            payload, evaluation = qualify(args.output.parent, trace, public_events)
        except Exception as error:  # preserve failure evidence at the process boundary
            payload = {
                "schema_version": 2,
                "status": "failed",
                "recorded_at": datetime.now(UTC).isoformat(),
                "error_type": type(error).__name__,
                "error": str(error),
                "trace_action_count": len(trace),
                "claim_boundary": "No source association capability is claimed.",
            }
            _write_trace(args.output.parent / "trace.jsonl", trace)
            _write_jsonl(args.output.parent / "public.jsonl", public_events)
            _write_create_only(args.output, payload)
            print(f"Harbor mirrors failed: {error}")
            return 1
        _write_trace(args.output.parent / "trace.jsonl", trace)
        _write_jsonl(args.output.parent / "public.jsonl", public_events)
        _write_jsonl(args.output.parent / "evaluation.jsonl", [evaluation])
        _write_create_only(args.output, payload)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    finally:
        rclpy.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
