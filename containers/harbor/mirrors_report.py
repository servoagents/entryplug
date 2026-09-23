#!/usr/bin/python3
"""Build observer-only Hall of Mirrors visuals from completed run artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import cv2
import numpy as np


def _read_rgb(path: Path) -> np.ndarray:
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError(f"could not read observer report input: {path}")
    return cv2.cvtColor(image, cv2.COLOR_BGR2RGB)


def _write_rgb_create_only(path: Path, rgb: np.ndarray) -> None:
    encoded, contents = cv2.imencode(".png", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
    if not encoded:
        raise RuntimeError(f"OpenCV could not encode observer report image: {path}")
    with path.open("xb") as stream:
        stream.write(contents.tobytes())


def _put_outlined_text(
    image: np.ndarray,
    text: str,
    origin: tuple[int, int],
    scale: float,
    color: tuple[int, int, int],
) -> None:
    cv2.putText(
        image,
        text,
        origin,
        cv2.FONT_HERSHEY_SIMPLEX,
        scale,
        (0, 0, 0),
        4,
        cv2.LINE_AA,
    )
    cv2.putText(
        image,
        text,
        origin,
        cv2.FONT_HERSHEY_SIMPLEX,
        scale,
        color,
        2,
        cv2.LINE_AA,
    )


def _spectator_panel(rgb: np.ndarray) -> np.ndarray:
    panel = cv2.resize(rgb, (800, 600))
    height, width = panel.shape[:2]
    red = panel[:, :, 0].astype(np.int16)
    green = panel[:, :, 1].astype(np.int16)
    blue = panel[:, :, 2].astype(np.int16)
    red_mask = (red >= 150) & (red >= green + 70) & (red >= blue + 70)
    blue_mask = (blue >= 150) & (blue >= red + 70) & (blue >= green + 70)

    def band_center(mask: np.ndarray, use_maximum: bool) -> tuple[int, int]:
        rows, columns = np.nonzero(mask)
        if len(rows) < 20:
            raise RuntimeError(
                "spectator articulation marker could not find arm pixels"
            )
        edge = int(np.max(rows) if use_maximum else np.min(rows))
        band = np.abs(rows - edge) <= max(2, height // 120)
        return int(round(float(np.mean(columns[band])))), int(
            round(float(np.mean(rows[band])))
        )

    cv2.rectangle(panel, (0, 0), (width, 42), (0, 0, 0), -1)
    cv2.rectangle(panel, (0, height - 34), (width, height), (0, 0, 0), -1)
    _put_outlined_text(
        panel,
        "OBSERVER ONLY: ARTICULATED MECHANISMS",
        (16, 29),
        0.66,
        (255, 255, 255),
    )
    _put_outlined_text(
        panel,
        "CAPTURED AFTER ASSOCIATION EXITED",
        (16, height - 10),
        0.5,
        (255, 255, 255),
    )

    halves = (
        ("CONTROLLED", 0, width // 2),
        ("INDEPENDENT", width // 2, width),
    )
    for name, left, right in halves:
        region = np.zeros((height, width), dtype=bool)
        region[:, left:right] = True
        base = band_center(red_mask & region, use_maximum=False)
        red_elbow = band_center(red_mask & region, use_maximum=True)
        blue_elbow = band_center(blue_mask & region, use_maximum=False)
        elbow = (
            int(round((red_elbow[0] + blue_elbow[0]) / 2)),
            int(round((red_elbow[1] + blue_elbow[1]) / 2)),
        )
        camera = band_center(blue_mask & region, use_maximum=True)

        cv2.circle(panel, base, 9, (255, 255, 255), 3, cv2.LINE_AA)
        cv2.circle(panel, elbow, 10, (255, 221, 64), 3, cv2.LINE_AA)
        cv2.circle(panel, camera, 11, (45, 212, 191), 3, cv2.LINE_AA)
        cv2.circle(panel, camera, 4, (4, 17, 29), -1, cv2.LINE_AA)
        cv2.circle(
            panel,
            (camera[0] - 3, camera[1] - 3),
            2,
            (255, 255, 255),
            -1,
            cv2.LINE_AA,
        )

        name_x = max(left + 8, min(right - 155, base[0] - 65))
        _put_outlined_text(
            panel,
            name,
            (name_x, max(65, base[1] - 24)),
            0.5,
            (255, 255, 255),
        )
        _put_outlined_text(
            panel, "base", (base[0] + 12, base[1] + 5), 0.4, (255, 255, 255)
        )
        _put_outlined_text(
            panel,
            "elbow",
            (elbow[0] + 12, elbow[1] + 5),
            0.4,
            (255, 221, 64),
        )
        _put_outlined_text(
            panel,
            "camera",
            (camera[0] + 12, camera[1] + 5),
            0.4,
            (45, 212, 191),
        )
    return panel


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_report(run_dir: Path) -> None:
    spectator_path = run_dir / "spectator-overview.raw.png"
    agent_views_path = run_dir / "agent-views.observer.png"
    trace_path = run_dir / "probe-response-trace.observer.png"
    inputs = (spectator_path, agent_views_path, trace_path)

    spectator = _spectator_panel(_read_rgb(spectator_path))
    agent_views = _read_rgb(agent_views_path)
    trace = _read_rgb(trace_path)
    if agent_views.shape[:2] != (600, 400):
        raise RuntimeError(f"unexpected agent view panel shape: {agent_views.shape}")
    if trace.shape[:2] != (300, 1280):
        raise RuntimeError(f"unexpected probe trace shape: {trace.shape}")

    _write_rgb_create_only(run_dir / "spectator-overview.observer.png", spectator)
    canvas = np.full((900, 1280, 3), (8, 13, 24), dtype=np.uint8)
    canvas[0:600, 0:800] = spectator
    canvas[0:600, 840:1240] = agent_views
    canvas[600:900, 0:1280] = trace
    _write_rgb_create_only(run_dir / "hall-of-mirrors-demo.observer.png", canvas)

    report = {
        "schema_version": 1,
        "status": "passed",
        "recorded_at": datetime.now(UTC).isoformat(),
        "boundary": (
            "Generated by an evaluator-only process after the association process "
            "exited. The spectator topic was not available to source selection."
        ),
        "inputs": {path.name: _sha256(path) for path in inputs},
        "outputs": [
            "spectator-overview.observer.png",
            "hall-of-mirrors-demo.observer.png",
        ],
    }
    with (run_dir / "observer-report.json").open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, sort_keys=True)
        stream.write("\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    build_report(args.run_dir)
    print(json.dumps({"status": "passed", "run_dir": str(args.run_dir)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
