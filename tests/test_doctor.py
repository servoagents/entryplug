from __future__ import annotations

import importlib.metadata
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence

from entryplug.doctor import ProbeContext, read_os_release, run_doctor, write_report


def _profile(path: Path) -> Path:
    data = {
        "architecture": "x86_64",
        "os": {"id": "ubuntu", "version_id": "24.04"},
        "profile_id": "test-profile",
        "python": {"executable": "/usr/bin/python3", "major": 3, "minor": 12},
        "required_commands": ["uv", "ros2"],
        "required_imports": [
            {"distribution": "numpy", "module": "numpy"},
            {"distribution": "mcp", "module": "mcp"},
        ],
        "ros": {"distro": "jazzy", "required_packages": ["rmw_zenoh_cpp"]},
        "selection": {
            "a2a_sdk": "1.2.3",
            "mcp_sdk": "1.2.3",
            "mujoco_ros2_control_commit": "abc123",
            "qualification": "qualified",
            "ros_apt_versions": {"ros-jazzy-rclpy": "1.2.3"},
        },
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class _Spec:
    def __init__(self, origin: str) -> None:
        self.origin = origin


def _context(*, healthy: bool) -> ProbeContext:
    modules = {
        "numpy": _Spec("/opt/ros/jazzy/numpy/__init__.py"),
        "mcp": _Spec("/venv/mcp/__init__.py"),
        "rclpy": _Spec("/opt/ros/jazzy/rclpy/__init__.py"),
        "cv_bridge": _Spec("/opt/ros/jazzy/cv_bridge/__init__.py"),
        "sensor_msgs": _Spec("/opt/ros/jazzy/sensor_msgs/__init__.py"),
    }

    def find_spec(module: str) -> object | None:
        if not healthy and module in {"mcp", "rclpy", "cv_bridge", "sensor_msgs"}:
            return None
        return modules.get(module)

    def version(distribution: str) -> str:
        if distribution in {"numpy", "mcp"}:
            return "1.2.3"
        raise importlib.metadata.PackageNotFoundError(distribution)

    def run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, "/opt/ros/jazzy\n", "")

    return ProbeContext(
        os_release={"ID": "ubuntu", "VERSION_ID": "24.04" if healthy else "26.04"},
        machine="x86_64",
        python_version=(3, 12, 8) if healthy else (3, 14, 4),
        python_executable="/usr/bin/python3",
        environ={"ROS_DISTRO": "jazzy"} if healthy else {},
        which=(lambda command: f"/usr/bin/{command}" if healthy else None),
        find_spec=find_spec,
        distribution_version=version,
        run=run,
        image_roundtrip=lambda: (healthy, "pixel-identical round trip"),
    )


def test_red_report_preserves_failures_and_blockers(tmp_path: Path) -> None:
    report = run_doctor(
        _profile(tmp_path / "profile.json"),
        context=_context(healthy=False),
        now=datetime(2026, 9, 22, 12, 0, tzinfo=UTC),
    )

    assert report.overall == "red"
    by_id = {check.check_id: check for check in report.checks}
    assert by_id["host.os"].status == "fail"
    assert by_id["import.mcp"].status == "missing"
    assert by_id["ros.package.rmw_zenoh_cpp"].status == "blocked"
    assert by_id["ros.image_conversion"].status == "blocked"


def test_healthy_prerequisites_are_green(tmp_path: Path) -> None:
    report = run_doctor(_profile(tmp_path / "profile.json"), context=_context(healthy=True))

    assert report.overall == "green"
    assert all(check.status == "pass" for check in report.checks)


def test_unqualified_version_selection_cannot_be_green(tmp_path: Path) -> None:
    profile = _profile(tmp_path / "profile.json")
    data = json.loads(profile.read_text(encoding="utf-8"))
    data["selection"]["qualification"] = "unqualified"
    data["selection"]["mcp_sdk"] = None
    profile.write_text(json.dumps(data), encoding="utf-8")

    report = run_doctor(profile, context=_context(healthy=True))

    assert report.overall == "red"
    by_id = {check.check_id: check for check in report.checks}
    assert by_id["profile.version_lock"].status == "blocked"
    assert "mcp_sdk" in by_id["profile.version_lock"].observed


def test_report_is_create_only_and_records_path(tmp_path: Path) -> None:
    report = run_doctor(_profile(tmp_path / "profile.json"), context=_context(healthy=False))
    destination = tmp_path / "run" / "manifest.json"

    recorded = write_report(report, destination)

    assert recorded.report_path == str(destination)
    saved = json.loads(destination.read_text(encoding="utf-8"))
    assert saved["overall"] == "red"
    assert saved["report_path"] == str(destination)
    try:
        write_report(report, destination)
    except FileExistsError:
        pass
    else:
        raise AssertionError("doctor evidence was overwritten")


def test_os_release_parser_does_not_execute_values(tmp_path: Path) -> None:
    source = tmp_path / "os-release"
    source.write_text('ID=ubuntu\nVERSION_ID="24.04"\nNAME="Ubuntu $(false)"\n', encoding="utf-8")

    assert read_os_release(source) == {
        "ID": "ubuntu",
        "VERSION_ID": "24.04",
        "NAME": "Ubuntu $(false)",
    }
