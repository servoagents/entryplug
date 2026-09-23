#!/usr/bin/python3
"""Qualify active source association against delayed and unrelated visual paths."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import time
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import rclpy

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
FIXTURE_SEED = 3119
SOURCE_NAME_SEED = 7043
DELAY_SECONDS = 0.36


class IndependentVisualFixture:
    """A prerecorded visual schedule that is independent of solver probes."""

    def __init__(self, seed: int, samples: int = 64) -> None:
        generator = random.Random(seed)
        position = 250.0
        positions: list[float] = []
        for _ in range(samples):
            position = max(232.0, min(268.0, position + generator.uniform(-4.5, 4.5)))
            positions.append(position)
        self._positions = tuple(positions)
        self._index = 0
        self._started = time.monotonic()

    def sample(self) -> tuple[dict[str, object], Frame]:
        if self._index >= len(self._positions):
            raise RuntimeError("independent visual fixture exhausted its prerecorded schedule")
        y_center = self._positions[self._index]
        self._index += 1
        height, width = 480, 640
        rgb = np.empty((height, width, 3), dtype=np.uint8)
        rgb[:, :, :] = (32, 52, 70)
        apex = int(round(y_center + 8.0))
        for row in range(apex, height):
            half_width = min(width // 2, int((row - apex) * 1.35))
            if half_width:
                rgb[row, width // 2 - half_width : width // 2 + half_width, :] = (
                    0,
                    0,
                    220,
                )
        top = max(0, int(round(y_center - 5.0)))
        bottom = min(height, int(round(y_center + 5.0)))
        rgb[top:bottom, width // 2 - 20 : width // 2 + 20, :] = (205, 0, 0)
        contents = rgb.tobytes()
        elapsed_ns = int((time.monotonic() - self._started) * 1_000_000_000)
        frame = Frame(
            sequence=self._index,
            received_monotonic=time.monotonic(),
            width=width,
            height=height,
            encoding="rgb8",
            step=width * 3,
            stamp_sec=elapsed_ns // 1_000_000_000,
            stamp_nanosec=elapsed_ns % 1_000_000_000,
            sha256=hashlib.sha256(contents).hexdigest(),
            data=contents,
        )
        return _marker(frame), frame

    def effect(self) -> tuple[float, dict[str, object]]:
        before, before_frame = self.sample()
        after, after_frame = self.sample()
        observed = time.monotonic()
        return (
            _round(float(after["y_px"]) - float(before["y_px"])),
            {
                "before": before,
                "after": after,
                "maximum_age_ms": _round(
                    max(
                        observed - before_frame.received_monotonic,
                        observed - after_frame.received_monotonic,
                    )
                    * 1000
                ),
            },
        )


def _opaque_ids() -> tuple[list[str], list[str]]:
    generator = random.Random(SOURCE_NAME_SEED)
    candidate_ids = [f"view-{generator.getrandbits(32):08x}" for _ in range(4)]
    lineage_ids = [f"lineage-{generator.getrandbits(48):012x}" for _ in range(3)]
    return candidate_ids, lineage_ids


def _deliver_delayed_copy(
    node: Observer,
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
    node = Observer("entryplug_hall_of_mirrors")
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
        _write_png_create_only(output_dir / "mirrors-start.png", node.frame)
        anchor, visibility_anchor = _establish_visibility_anchor(node, trace)
        direct_noise = _no_action_noise(node)

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

        fixture = IndependentVisualFixture(FIXTURE_SEED)
        independent_noise_samples = [fixture.sample()[0] for _ in range(8)]
        independent_y = [float(sample["y_px"]) for sample in independent_noise_samples]
        independent_noise_range = max(independent_y) - min(independent_y)
        _, independent_preview = fixture.sample()
        _write_png_create_only(output_dir / "mirrors-independent-source.png", independent_preview)

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
        for index, command in enumerate(fit_commands):
            probe = _measure_probe(
                node,
                anchor,
                command,
                purpose=f"mirrors-fit-{index + 1}",
                trace=trace,
            )
            direct_effect = float(probe["observed_feature_delta_px"])
            independent_effect, independent_frames = fixture.effect()
            trace_item = next(
                item for item in trace if item.get("request_id") == probe["request_id"]
            )
            direct_ages.append(float(trace_item["after_feature"]["last_receive_age_ms"]))
            independent_ages.append(float(independent_frames["maximum_age_ms"]))
            delayed = _deliver_delayed_copy(node, probe, trace_item)
            direct_fit.append(direct_effect)
            independent_fit.append(independent_effect)
            delayed_fit.append(delayed)
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
                    "before": independent_frames["before"]["frame"]["sequence"],
                    "after": independent_frames["after"]["frame"]["sequence"],
                },
            }
            fit_records.append(record)
            public_events.append(record)

        direct_validation: list[float] = []
        independent_validation: list[float] = []
        delayed_validation: list[dict[str, object]] = []
        validation_records: list[dict[str, object]] = []
        for index, command in enumerate(validation_commands):
            probe = _measure_probe(
                node,
                anchor,
                command,
                purpose=f"mirrors-validation-{index + 1}",
                trace=trace,
            )
            direct_effect = float(probe["observed_feature_delta_px"])
            independent_effect, independent_frames = fixture.effect()
            trace_item = next(
                item for item in trace if item.get("request_id") == probe["request_id"]
            )
            direct_ages.append(float(trace_item["after_feature"]["last_receive_age_ms"]))
            independent_ages.append(float(independent_frames["maximum_age_ms"]))
            delayed = _deliver_delayed_copy(node, probe, trace_item)
            direct_validation.append(direct_effect)
            independent_validation.append(independent_effect)
            delayed_validation.append(delayed)
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
                    "before": independent_frames["before"]["frame"]["sequence"],
                    "after": independent_frames["after"]["frame"]["sequence"],
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
            noise_range_px=independent_noise_range,
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

        _return_to_anchor(node, anchor, purpose="mirrors:final-anchor", trace=trace)
        assert node.frame is not None
        _write_png_create_only(output_dir / "mirrors-end.png", node.frame)

        role_map = {
            controlled_id: "controlled_camera",
            delayed_id: "delayed_controlled_lineage",
            independent_id: "independent_visual_schedule",
            ambiguous_id: "negative_test_indistinguishable_clone",
        }
        assessments = {
            item["candidate_id"]: item for item in selection["assessments"]
        }
        gates = {
            "controlled_source_selected": selection.get("candidate_id") == controlled_id,
            "metadata_alone_refused": metadata_baseline["status"] == "refused",
            "delayed_path_rejected_as_stale": "stale"
            in assessments[delayed_id]["reasons"],
            "delayed_path_preserved_lineage": delayed.lineage_id == controlled.lineage_id,
            "independent_source_rejected": not assessments[independent_id]["eligible"],
            "ambiguous_sources_refused": ambiguity_check.get("reason_code")
            == "ambiguous_sources",
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
                "Source roles and the independent schedule seed are fixture evaluation data; "
                "they were not inputs to candidate assessment or selection."
            ),
        }
        summary = {
            "schema_version": 1,
            "status": "passed",
            "recorded_at": datetime.now(UTC).isoformat(),
            "seeds": {
                "solver": SOLVER_SEED,
                "fixture": FIXTURE_SEED,
                "source_names": SOURCE_NAME_SEED,
                "independent": True,
            },
            "interfaces": {
                "camera_topic": "/camera/color/image_raw",
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
                "independent_path_y_range_px": _round(independent_noise_range),
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
                "public_events": "public.jsonl",
                "private_evaluation": "evaluation.jsonl",
                "action_trace": "trace.jsonl",
            },
            "timing_ms": {"total": _round((time.monotonic() - started) * 1000)},
            "claim_boundary": (
                "The controlled path uses rendered ROS images and real bounded actions. "
                "The independent distractor is a generated visual fixture, not a second "
                "MuJoCo camera. This qualifies association behavior, not the complete "
                "two-camera Hall of Mirrors."
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
                "schema_version": 1,
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
