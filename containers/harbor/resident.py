#!/usr/bin/python3
"""Private single-world Harbor task worker; the host remains operation authority."""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import cv2
import numpy as np
import rclpy
import reach
from reach import (
    FIT_PROBES_RADIANS,
    RED_MARKER_DETECTOR_ID,
    TARGET_TOLERANCE_PX,
    VALIDATION_PROBES_RADIANS,
    FixedRedCentroidDetector,
    Observer,
    ReachCanceled,
    _establish_visibility_anchor,
    _fit_gain,
    _fresh_observation,
    _measure_probe,
    _no_action_noise,
    _reach_target,
    _round,
    _spin_until,
    _validate_gain,
)
from resident_evaluator import score_raw_completion
from smoke import _current_positions, _wait_stationary, _write_create_only, _write_png_create_only

from entryplug.association import (
    CachedVisualBinding,
    CandidateEvidence,
    load_visual_binding,
    make_visual_binding_record,
    select_candidate,
    validate_cached_binding,
)
from entryplug.detector_worker import ActiveDetectorPath, DetectorWorker, WorkerUnavailable
from entryplug.harbor_resident import MAX_RECORD_BYTES, SOURCE_ID, SOURCE_LINEAGE
from entryplug.visual_task import visual_reach_arguments

REUSE_PROBES_RADIANS = (0.04, -0.04)
REPAIR_BUDGET_SECONDS = 20.0
SOURCE_TOPIC = "/camera/color/image_raw"


def _send(record: dict[str, object]) -> None:
    encoded = json.dumps(record, allow_nan=False, separators=(",", ":"))
    if len(encoded.encode()) + 1 > MAX_RECORD_BYTES:
        raise ValueError("resident output exceeds record bound")
    sys.stdout.write(encoded + "\n")
    sys.stdout.flush()


def _reader(
    pending: queue.Queue[dict[str, object] | None],
    active: dict[str, object],
    lock: threading.Lock,
    client_gone: threading.Event,
) -> None:
    while True:
        line = sys.stdin.buffer.readline(MAX_RECORD_BYTES + 1)
        if not line or len(line) > MAX_RECORD_BYTES:
            client_gone.set()
            with lock:
                event = active.get("cancel")
                if isinstance(event, threading.Event):
                    event.set()
            pending.put(None)
            return
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            client_gone.set()
            pending.put(None)
            return
        if not isinstance(record, dict) or record.get("version") != 1:
            client_gone.set()
            pending.put(None)
            return
        kind = record.get("type")
        operation_id = record.get("operation_id")
        if not isinstance(operation_id, str) or not operation_id:
            client_gone.set()
            pending.put(None)
            return
        if kind == "cancel":
            with lock:
                if active.get("operation_id") == operation_id:
                    event = active.get("cancel")
                    if isinstance(event, threading.Event):
                        event.set()
                else:
                    early = active.setdefault("pending_cancels", set())
                    if isinstance(early, set):
                        early.add(operation_id)
            continue
        if kind != "reach":
            client_gone.set()
            pending.put(None)
            return
        pending.put(record)


def _write_trace(path: Path, trace: list[dict[str, object]]) -> None:
    with path.open("x", encoding="utf-8") as stream:
        for item in trace:
            stream.write(json.dumps(item, allow_nan=False, sort_keys=True) + "\n")


def _repair_binding(
    node: Observer,
    anchor: dict[str, float],
    binding: CachedVisualBinding,
    paths: ActiveDetectorPath,
    trace: list[dict[str, object]],
    prefix: str,
    cancel: threading.Event,
    deadline_monotonic: float,
) -> dict[str, object]:
    """Validate the prepared alternate under the same motion owner and deadline."""
    started = time.monotonic()
    budget_end = min(deadline_monotonic, started + REPAIR_BUDGET_SECONDS)
    stationary = _wait_stationary(node, timeout=3.0)
    if cancel.is_set() or time.monotonic() >= budget_end:
        raise WorkerUnavailable("REPAIR_BUDGET_EXHAUSTED")
    paths.select_alternate(quiescence_confirmed=stationary["confirmed"] is True)
    first = _fresh_observation(node)
    worker = first["worker"]
    if not isinstance(worker, dict) or (
        worker.get("source_id") != SOURCE_ID
        or worker.get("lineage_id") != SOURCE_LINEAGE
        or worker.get("detector_id") != RED_MARKER_DETECTOR_ID
        or worker.get("interface_version") != "1"
        or worker.get("worker_instance") != paths.alternate.instance
        or worker.get("worker_generation") != paths.alternate.generation
    ):
        raise WorkerUnavailable("REPLACEMENT_SOURCE_INCOMPATIBLE")
    checks: list[dict[str, object]] = []
    for delta in REUSE_PROBES_RADIANS:
        if cancel.is_set() or time.monotonic() >= budget_end:
            raise WorkerUnavailable("REPAIR_BUDGET_EXHAUSTED")
        checks.append(
            _measure_probe(
                node,
                anchor,
                delta,
                purpose=f"{prefix}:replacement-check-{len(checks) + 1}",
                trace=trace,
                cancel_requested=lambda: cancel.is_set() or time.monotonic() >= budget_end,
            )
        )
    last = _fresh_observation(node)
    validation = validate_cached_binding(
        binding,
        commands_radians=REUSE_PROBES_RADIANS,
        candidate_effects_px={
            SOURCE_ID: [float(item["observed_feature_delta_px"]) for item in checks]
        },
        candidate_lineages={SOURCE_ID: SOURCE_LINEAGE},
        candidate_maximum_ages_ms={SOURCE_ID: float(last["last_receive_age_ms"])},
        candidate_noise_ranges_px={SOURCE_ID: binding.noise_range_px},
    )
    if validation["status"] != "reused":
        raise WorkerUnavailable("REPLACEMENT_VALIDATION_FAILED")
    if cancel.is_set() or time.monotonic() >= budget_end:
        raise WorkerUnavailable("REPAIR_BUDGET_EXHAUSTED")
    revision = paths.commit_binding_revision()
    return {
        "status": "validated",
        "binding_revision": revision,
        "old_binding_revision_invalidated": True,
        "worker": last["worker"],
        "validation": validation,
        "validation_probe_count": len(checks),
        "quiescence": stationary,
        "repair_ms": _round((time.monotonic() - started) * 1000),
    }


def _task(
    node: Observer,
    anchor: dict[str, float],
    binding: object,
    run_dir: Path,
    index: int,
    operation_id: str,
    target_y_px: float,
    cancel: threading.Event,
    paths: ActiveDetectorPath,
    evaluation_fault: str | None,
    deadline_monotonic: float,
) -> dict[str, object]:
    from entryplug.association import CachedVisualBinding

    assert isinstance(binding, CachedVisualBinding)
    started = time.monotonic()
    trace: list[dict[str, object]] = []
    prefix = f"task-{index:04d}"
    fault_event: dict[str, object] = {"applied": False}
    recovery: dict[str, object] | None = None

    def on_goal_accepted(purpose: str, goal_id: str) -> None:
        if evaluation_fault is None or fault_event["applied"]:
            return
        if purpose != f"{prefix}:servo-step-1":
            return
        fault_event.update(
            applied=True,
            phase="native_correction_accepted",
            goal_id=goal_id,
            applied_monotonic=time.monotonic(),
            primary_instance=paths.primary.instance,
        )
        if evaluation_fault == "kill-active-worker-stale-alternate":
            paths.alternate.set_evaluation_delay(0.5)
            fault_event["alternate_observation_delay_s"] = 0.5
        paths.primary.kill_for_evaluation()
        if evaluation_fault == "kill-both-workers":
            paths.alternate.kill_for_evaluation()

    observation = _fresh_observation(node)
    assert node.frame is not None
    _write_png_create_only(run_dir / f"{prefix}-before.raw.png", node.frame)
    before_rgb = (
        np.frombuffer(node.frame.data, dtype=np.uint8)
        .reshape((node.frame.height, node.frame.width, 3))
        .copy()
    )
    reticle = before_rgb.copy()
    cv2.line(
        reticle,
        (0, round(target_y_px)),
        (node.frame.width - 1, round(target_y_px)),
        (170, 60, 210),
        1,
    )
    cv2.putText(
        reticle,
        f"TARGET ROW {target_y_px:.1f}",
        (12, 24),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.5,
        (170, 60, 210),
        1,
    )
    ok, png = cv2.imencode(".png", cv2.cvtColor(reticle, cv2.COLOR_RGB2BGR))
    if not ok:
        raise RuntimeError("reticle encoding failed")
    with (run_dir / f"{prefix}-reticle.observer.png").open("xb") as stream:
        stream.write(png.tobytes())

    checks: list[dict[str, object]] = []
    validation = None
    trial: dict[str, object] = {}
    status = "failed"
    reason = "TARGET_NOT_REACHED"
    if cancel.is_set():
        status, reason = "canceled", "CANCEL_REQUESTED"
    else:
        for delta in REUSE_PROBES_RADIANS:
            if cancel.is_set():
                break
            try:
                checks.append(
                    _measure_probe(
                        node,
                        anchor,
                        delta,
                        purpose=f"{prefix}:current-binding-check-{len(checks) + 1}",
                        trace=trace,
                        cancel_requested=cancel.is_set,
                    )
                )
            except ReachCanceled:
                status, reason = "canceled", "CANCEL_REQUESTED"
                break
        if cancel.is_set():
            status, reason = "canceled", "CANCEL_REQUESTED"
        else:
            validation = validate_cached_binding(
                binding,
                commands_radians=REUSE_PROBES_RADIANS,
                candidate_effects_px={
                    SOURCE_ID: [float(item["observed_feature_delta_px"]) for item in checks]
                },
                candidate_lineages={SOURCE_ID: SOURCE_LINEAGE},
                candidate_maximum_ages_ms={
                    SOURCE_ID: float(_fresh_observation(node)["last_receive_age_ms"])
                },
                candidate_noise_ranges_px={SOURCE_ID: binding.noise_range_px},
            )
            if validation["status"] != "reused":
                status, reason = "refused", "BINDING_CHECK_FAILED"
            elif not cancel.is_set():
                try:
                    trial = _reach_target(
                        node,
                        anchor,
                        gain=binding.gain_px_per_radian,
                        target_y_px=target_y_px,
                        calibration_source=binding.evidence_key,
                        purpose=prefix,
                        trace=trace,
                        cancel_requested=cancel.is_set,
                        on_goal_accepted=on_goal_accepted if evaluation_fault else None,
                    )
                except WorkerUnavailable as loss:
                    detected_at = time.monotonic()
                    if fault_event["applied"]:
                        fault_event["detected_monotonic"] = detected_at
                        fault_event["fault_to_detection_ms"] = _round(
                            (detected_at - float(fault_event["applied_monotonic"])) * 1000
                        )
                    recovery = {
                        "status": "loss_detected",
                        "loss_reason": loss.reason_code,
                    }
                    try:
                        recovered = _repair_binding(
                            node,
                            anchor,
                            binding,
                            paths,
                            trace,
                            prefix,
                            cancel,
                            deadline_monotonic,
                        )
                        recovery.update(recovered)
                        if cancel.is_set():
                            raise ReachCanceled(prefix)
                        trial = _reach_target(
                            node,
                            anchor,
                            gain=binding.gain_px_per_radian,
                            target_y_px=target_y_px,
                            calibration_source=binding.evidence_key,
                            purpose=f"{prefix}:resumed",
                            trace=trace,
                            cancel_requested=cancel.is_set,
                        )
                    except ReachCanceled:
                        if cancel.is_set():
                            trial = {"status": "canceled", "reason_code": "CANCEL_REQUESTED"}
                        else:
                            recovery["status"] = "unavailable"
                            recovery["failure_reason"] = "REPAIR_BUDGET_EXHAUSTED"
                            trial = {
                                "status": "failed",
                                "reason_code": "VISUAL_CAPABILITY_UNAVAILABLE",
                            }
                    except WorkerUnavailable as repair_error:
                        recovery["status"] = "unavailable"
                        recovery["failure_reason"] = repair_error.reason_code
                        trial = {
                            "status": "failed",
                            "reason_code": "VISUAL_CAPABILITY_UNAVAILABLE",
                        }
                status = str(trial["status"])
                reason = str(
                    trial.get(
                        "reason_code",
                        "TARGET_REACHED" if status == "passed" else "TARGET_NOT_REACHED",
                    )
                )
    stationary = _wait_stationary(node, timeout=3.0)
    try:
        end = _fresh_observation(node)
    except WorkerUnavailable as final_loss:
        if status not in {"failed", "canceled"}:
            raise
        end = None
        if status == "failed":
            reason = "VISUAL_CAPABILITY_UNAVAILABLE"
            recovery = recovery or {
                "status": "unavailable",
                "failure_reason": final_loss.reason_code,
            }
    assert node.frame is not None
    _write_png_create_only(run_dir / f"{prefix}-after.raw.png", node.frame)
    error = target_y_px - float(end["y_px"]) if end is not None else None
    if status == "passed" and error is not None and abs(error) > TARGET_TOLERANCE_PX:
        status, reason = "failed", "FALSE_VISUAL_COMPLETION"
    result = {
        "version": 1,
        "type": "result",
        "operation_id": operation_id,
        "status": status,
        "reason_code": reason,
        "source_id": SOURCE_ID,
        "lineage_id": SOURCE_LINEAGE,
        "source_topic": SOURCE_TOPIC,
        "target_y_px": _round(target_y_px),
        "initial_y_px": observation["y_px"],
        "final_y_px": end["y_px"] if end is not None else None,
        "final_error_px": _round(error) if error is not None else None,
        "tolerance_px": TARGET_TOLERANCE_PX,
        "binding_evidence_key": binding.evidence_key,
        "binding_revision": paths.binding_revision,
        "binding_reference": (
            f"{binding.evidence_key}:revision-{paths.binding_revision}" if end is not None else None
        ),
        "binding_ready": end is not None
        and validation is not None
        and validation["status"] == "reused"
        and (recovery is None or recovery["status"] == "validated"),
        "recovery": recovery,
        "detector_worker": end["worker"] if end is not None else None,
        "visual_capability_available": end is not None,
        "validation": validation,
        "validation_probe_count": len(checks),
        "validation_ms": _round(
            (time.monotonic() - started) * 1000 - float(trial.get("timing_ms", 0))
        ),
        "task_ms": trial.get("timing_ms", 0),
        "wall_ms": _round((time.monotonic() - started) * 1000),
        "action_count": len(trace),
        "commanded_travel_radians": _round(
            sum(abs(float(item["requested_delta_radians"]["joint2"])) for item in trace)
        ),
        "quiescence_confirmed": stationary["confirmed"],
        "trace_path": f"{prefix}-trace.jsonl",
        "reticle_path": f"{prefix}-reticle.observer.png",
    }
    if end is None:
        evaluator = {
            "status": "not_scored_without_detector",
            "independently_inside_tolerance": False,
            "false_visual_completion": False,
        }
    else:
        evaluator = score_raw_completion(
            node.frame,
            target_y_px=target_y_px,
            reported_y_px=float(end["y_px"]),
            claimed_success=status == "passed",
            tolerance_px=TARGET_TOLERANCE_PX,
            joint_feedback_radians=_current_positions(node),
        )
    _write_trace(run_dir / f"{prefix}-trace.jsonl", trace)
    _write_create_only(
        run_dir / f"{prefix}.json",
        {
            **result,
            "trial": trial,
            "checks": checks,
            "recovery": recovery,
            "fault_evaluator_only": fault_event,
            "evaluator_only": evaluator,
        },
    )
    return result


def _prepare_detector_paths() -> ActiveDetectorPath:
    primary = DetectorWorker(
        FixedRedCentroidDetector,
        detector_id=RED_MARKER_DETECTOR_ID,
        generation=1,
        source_id=SOURCE_ID,
        lineage_id=SOURCE_LINEAGE,
    )
    try:
        alternate = DetectorWorker(
            FixedRedCentroidDetector,
            detector_id=RED_MARKER_DETECTOR_ID,
            generation=2,
            source_id=SOURCE_ID,
            lineage_id=SOURCE_LINEAGE,
        )
    except Exception:
        primary.close()
        raise
    return ActiveDetectorPath(primary, alternate)


def serve(run_dir: Path, run_id: str) -> None:
    acquisition_started = time.monotonic()
    node = Observer("entryplug_resident_reach")
    detector_paths: ActiveDetectorPath | None = None
    try:
        _spin_until(
            node,
            lambda: node.frame is not None and node.joints is not None,
            30.0,
            "resident camera and joint topics",
        )
        if not node.action.wait_for_server(timeout_sec=15.0):
            raise TimeoutError("resident trajectory action was unavailable")
        frame_sequence_before_spawn = node.frame.sequence if node.frame is not None else 0
        detector_paths = _prepare_detector_paths()
        reach._MARKER_DETECTOR = detector_paths
        _spin_until(
            node,
            lambda: (
                node.frame is not None
                and node.frame.sequence > frame_sequence_before_spawn
                and node.get_clock().now().nanoseconds
                >= node.frame.stamp_sec * 1_000_000_000 + node.frame.stamp_nanosec
            ),
            8.0,
            "fresh ROS clock after detector worker preparation",
        )
        trace: list[dict[str, object]] = []
        anchor, visibility = _establish_visibility_anchor(node, trace)
        noise = _no_action_noise(node)
        fit = [
            _measure_probe(node, anchor, delta, purpose=f"resident-fit-{index + 1}", trace=trace)
            for index, delta in enumerate(FIT_PROBES_RADIANS)
        ]
        model = _fit_gain(fit)
        gain = float(model["gain_px_per_radian"])
        held_out = [
            _measure_probe(
                node, anchor, delta, purpose=f"resident-validation-{index + 1}", trace=trace
            )
            for index, delta in enumerate(VALIDATION_PROBES_RADIANS)
        ]
        validation = _validate_gain(gain, held_out, noise_range_px=float(noise["y_range_px"]))
        if validation["status"] != "passed":
            raise RuntimeError("resident acquired mapping failed separate validation")
        candidate = CandidateEvidence(
            candidate_id=SOURCE_ID,
            lineage_id=SOURCE_LINEAGE,
            maximum_age_ms=float(_fresh_observation(node)["last_receive_age_ms"]),
            noise_range_px=float(noise["y_range_px"]),
            fit_commands_radians=tuple(float(item["requested_delta_radians"]) for item in fit),
            fit_effects_px=tuple(float(item["observed_feature_delta_px"]) for item in fit),
            validation_commands_radians=tuple(
                float(item["requested_delta_radians"]) for item in held_out
            ),
            validation_effects_px=tuple(
                float(item["observed_feature_delta_px"]) for item in held_out
            ),
        )
        association = select_candidate((candidate,))
        if association.get("status") != "selected":
            raise RuntimeError("resident camera did not pass causal source selection")
        record = make_visual_binding_record(
            context={
                "task": "visual_reach",
                "fixture": "harbor-resident-v1",
                "source_convention": "red-centroid-y-v1",
            },
            candidate_id=SOURCE_ID,
            lineage_id=SOURCE_LINEAGE,
            gain_px_per_radian=gain,
            validity_radians=(
                min(FIT_PROBES_RADIANS + VALIDATION_PROBES_RADIANS),
                max(FIT_PROBES_RADIANS + VALIDATION_PROBES_RADIANS),
            ),
            noise_range_px=float(noise["y_range_px"]),
            maximum_age_ms=250.0,
            timing_method_id="settled-before-after-v1",
            evidence_refs=("resident-acquisition.json", "resident-acquisition-trace.jsonl"),
            created_at=datetime.now(UTC).isoformat(),
        )
        binding = load_visual_binding(record)
        initial = _fresh_observation(node)
        acquisition_ms = _round((time.monotonic() - acquisition_started) * 1000)
        _write_trace(run_dir / "resident-acquisition-trace.jsonl", trace)
        _write_create_only(
            run_dir / "resident-acquisition.json",
            {
                "status": "passed",
                "run_id": run_id,
                "source_id": SOURCE_ID,
                "lineage_id": SOURCE_LINEAGE,
                "source_topic": SOURCE_TOPIC,
                "visibility_anchor": visibility,
                "noise": noise,
                "fit_probes": fit,
                "mapping": model,
                "association": association,
                "validation_probes": held_out,
                "validation": validation,
                "binding": record.to_dict(),
                "initial_y_px": initial["y_px"],
                "acquisition_ms": acquisition_ms,
                "detector_worker": initial["worker"],
                "claim_boundary": (
                    "One configured camera passed intervention-based selection. "
                    "No comparison against alternative sources is claimed."
                ),
            },
        )
        _send(
            {
                "version": 1,
                "type": "ready",
                "run_id": run_id,
                "source_id": SOURCE_ID,
                "lineage_id": SOURCE_LINEAGE,
                "initial_y_px": initial["y_px"],
                "acquisition_ms": acquisition_ms,
                "detector_worker": initial["worker"],
            }
        )

        pending: queue.Queue[dict[str, object] | None] = queue.Queue(maxsize=2)
        active: dict[str, object] = {}
        lock = threading.Lock()
        client_gone = threading.Event()
        threading.Thread(
            target=_reader, args=(pending, active, lock, client_gone), daemon=True
        ).start()
        task_index = 0
        while not client_gone.is_set():
            command = pending.get()
            if command is None:
                break
            task_index += 1
            operation_id = str(command["operation_id"])
            cancel = threading.Event()
            with lock:
                active.update(operation_id=operation_id, cancel=cancel)
                early = active.pop("pending_cancels", set())
                if isinstance(early, set) and operation_id in early:
                    cancel.set()
            try:
                target = float(
                    visual_reach_arguments({"target_y_px": command.get("target_y_px")})[
                        "target_y_px"
                    ]
                )
                fault_value = command.get("evaluation_fault")
                if fault_value not in {
                    None,
                    "kill-active-worker",
                    "kill-both-workers",
                    "kill-active-worker-stale-alternate",
                }:
                    raise ValueError("unknown private evaluator fault")
                deadline_value = command.get("deadline_monotonic")
                if deadline_value is None:
                    deadline_value = time.monotonic() + 55.0
                if isinstance(deadline_value, bool) or not isinstance(deadline_value, (int, float)):
                    raise ValueError("resident operation deadline is invalid")
                assert detector_paths is not None
                result = _task(
                    node,
                    anchor,
                    binding,
                    run_dir,
                    task_index,
                    operation_id,
                    target,
                    cancel,
                    detector_paths,
                    fault_value,
                    float(deadline_value),
                )
            except Exception as error:
                print(
                    f"resident task {operation_id} failed: {type(error).__name__}: {error}",
                    file=sys.stderr,
                    flush=True,
                )
                raise
            finally:
                with lock:
                    active.clear()
            if not client_gone.is_set():
                _send(result)
    finally:
        if detector_paths is not None:
            detector_paths.close()
        node.destroy_node()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    rclpy.init()
    try:
        serve(args.run_dir, args.run_id)
        return 0
    finally:
        rclpy.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
