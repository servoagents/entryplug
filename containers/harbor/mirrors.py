#!/usr/bin/python3
"""Qualify active association across two rendered cameras and a delayed path."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import statistics
import time
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path

import cv2
import numpy as np
import rclpy
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import Image

from entryplug.association import (
    AssociationProfile,
    CandidateEvidence,
    metadata_only_selection,
    select_candidate,
)
from reach import (
    FIT_PROBES_RADIANS,
    VALIDATION_PROBES_RADIANS,
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


SOLVER_SEED = 947
SOURCE_NAME_SEED = 7043
DELAY_SECONDS = 0.36


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
        raise TimeoutError(
            f"received {len(observations)} of {samples} fresh mirror camera samples"
        )
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


def _annotated(frame: Frame, feature: dict[str, object], label: str) -> np.ndarray:
    rgb = np.frombuffer(frame.data, dtype=np.uint8).reshape(
        (frame.height, frame.width, 3)
    ).copy()
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
    encoded, contents = cv2.imencode(".png", cv2.cvtColor(review, cv2.COLOR_RGB2BGR))
    if not encoded:
        raise RuntimeError("OpenCV could not encode the review image")
    with path.open("xb") as stream:
        stream.write(contents.tobytes())


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record, sort_keys=True))
            stream.write("\n")


def _candidate_record(evidence: CandidateEvidence) -> dict[str, object]:
    return asdict(evidence)


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
                node.frame is not None
                and node.mirror_frame is not None
                and node.joints is not None
            ),
            30.0,
            "two cameras and joint topics",
        )
        if not node.action.wait_for_server(timeout_sec=15.0):
            raise TimeoutError("trajectory action server was unavailable")
        assert node.frame is not None
        controlled_at_rest = node.frame
        _write_png_create_only(
            output_dir / "controlled-at-rest.raw.png", controlled_at_rest
        )
        anchor, visibility_anchor = _establish_visibility_anchor(node, trace)
        controlled_anchor_feature = visibility_anchor["after_feature"]
        controlled_anchor = _find_frame(
            node.frames,
            int(controlled_anchor_feature["last_frame_sequence"]),
            "controlled anchor",
        )
        _write_png_create_only(
            output_dir / "controlled-anchor.raw.png", controlled_anchor
        )
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
        _write_png_create_only(
            output_dir / "independent-camera-preview.raw.png", mirror_preview
        )
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
        for index, command in enumerate(fit_commands):
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
            direct_ages.append(
                float(trace_item["after_feature"]["last_receive_age_ms"])
            )
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
            direct_ages.append(
                float(trace_item["after_feature"]["last_receive_age_ms"])
            )
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

        maximum_delay_ms = max(
            float(item["delay_ms"]) for item in delayed_fit + delayed_validation
        )
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
        selection = select_candidate(candidates, profile)

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
                "metadata_only_baseline": metadata_baseline,
                "ambiguity_check": ambiguity_check,
            }
        )

        representative_direct = max(
            direct_pairs, key=lambda item: abs(float(item["effect_px"]))
        )
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

        image_manifest = {
            "schema_version": 1,
            "raw_files_are_unmodified_sensor_captures": True,
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
                    "meaning": (
                        "controlled camera at the visible local operating anchor"
                    ),
                    "expected": (
                        "a measurable red patch appears beyond the blue forearm"
                    ),
                },
                "controlled-probe-before.raw.png": {
                    "kind": "raw",
                    "meaning": (
                        "controlled camera immediately before a representative probe"
                    ),
                },
                "controlled-probe-after.raw.png": {
                    "kind": "raw",
                    "meaning": "controlled camera immediately after that probe settled",
                },
                "independent-camera-preview.raw.png": {
                    "kind": "raw",
                    "meaning": (
                        "second MuJoCo camera on the independently driven mechanism"
                    ),
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
        assessments = {
            item["candidate_id"]: item for item in selection["assessments"]
        }
        gates = {
            "controlled_source_selected": (
                selection.get("candidate_id") == controlled_id
            ),
            "metadata_alone_refused": metadata_baseline["status"] == "refused",
            "delayed_path_rejected_as_stale": "stale"
            in assessments[delayed_id]["reasons"],
            "delayed_path_preserved_lineage": (
                delayed.lineage_id == controlled.lineage_id
            ),
            "independent_source_rejected": not assessments[independent_id]["eligible"],
            "ambiguous_sources_refused": ambiguity_check.get("reason_code")
            == "ambiguous_sources",
            "two_rendered_camera_topics_observed": (
                node.frame is not None and node.mirror_frame is not None
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
                "Source roles and the mirror driver schedule are fixture evaluation "
                "data; they were not inputs to candidate assessment or selection."
            ),
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
                "joint_topic": "/joint_states",
                "trajectory_action": "/trajectory_controller/follow_joint_trajectory",
            },
            "candidate_order": [item.candidate_id for item in candidates],
            "candidate_evidence": [_candidate_record(item) for item in candidates],
            "association": selection,
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
                    for item in delayed_fit + delayed_validation
                ),
                "same_lineage_as_controlled": True,
                "treated_as_independent_evidence": False,
            },
            "probe_count": len(fit_commands),
            "validation_probe_count": len(validation_commands),
            "fit_probes": fit_records,
            "validation_probes": validation_records,
            "safety": {
                "maximum_probe_radians": max(
                    abs(value) for value in fit_commands + validation_commands
                ),
                "all_native_actions_succeeded": all(
                    int(item["native_status"]) == 4 for item in trace
                ),
                "action_count": len(trace),
            },
            "artifacts": {
                "image_manifest": "images.json",
                "public_events": "public.jsonl",
                "private_evaluation": "evaluation.jsonl",
                "action_trace": "trace.jsonl",
            },
            "timing_ms": {"total": _round((time.monotonic() - started) * 1000)},
            "claim_boundary": (
                "Both candidate sources are real MuJoCo camera renders. The second "
                "mechanism follows a fixture-owned schedule independent of Entryplug "
                "probes. This qualifies local source association in this two-camera "
                "Harbor fixture, not general causal identification."
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
