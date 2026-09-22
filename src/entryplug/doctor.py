"""Read-only qualification checks for Entryplug's supported substrate."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
from collections import Counter
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Mapping, Sequence

PASS = "pass"
FAIL = "fail"
MISSING = "missing"
BLOCKED = "blocked"
ERROR = "error"

UNHEALTHY_STATUSES = frozenset({FAIL, MISSING, BLOCKED, ERROR})


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One independently useful substrate observation."""

    check_id: str
    status: str
    summary: str
    expected: str
    observed: str
    required: bool = True
    detail: str = ""
    remediation: str = ""


@dataclass(frozen=True, slots=True)
class DoctorReport:
    """Serializable evidence from one doctor invocation."""

    schema_version: int
    profile_id: str
    generated_at: str
    runtime_id: str
    overall: str
    counts: Mapping[str, int]
    checks: tuple[CheckResult, ...]
    report_path: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "profile_id": self.profile_id,
            "generated_at": self.generated_at,
            "runtime_id": self.runtime_id,
            "overall": self.overall,
            "counts": dict(sorted(self.counts.items())),
            "checks": [asdict(check) for check in self.checks],
            "report_path": self.report_path,
        }


@dataclass(frozen=True, slots=True)
class ProbeContext:
    """Injectable host access used to keep doctor tests deterministic."""

    os_release: Mapping[str, str]
    machine: str
    python_version: tuple[int, int, int]
    python_executable: str
    python_base_executable: str
    environ: Mapping[str, str]
    which: Callable[[str], str | None]
    find_spec: Callable[[str], object | None]
    distribution_version: Callable[[str], str]
    run: Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
    image_roundtrip: Callable[[], tuple[bool, str]]

    @classmethod
    def live(cls) -> ProbeContext:
        return cls(
            os_release=read_os_release(),
            machine=platform.machine(),
            python_version=(sys.version_info.major, sys.version_info.minor, sys.version_info.micro),
            python_executable=sys.executable,
            python_base_executable=getattr(sys, "_base_executable", sys.executable),
            environ=os.environ,
            which=shutil.which,
            find_spec=importlib.util.find_spec,
            distribution_version=importlib.metadata.version,
            run=_run_command,
            image_roundtrip=_live_image_roundtrip,
        )


def read_os_release(path: Path = Path("/etc/os-release")) -> dict[str, str]:
    """Parse the freedesktop os-release format without executing it."""

    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if value.startswith(('"', "'")) and value.endswith(value[:1]):
            value = value[1:-1]
        values[key] = value
    return values


def _run_command(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )


def _live_image_roundtrip() -> tuple[bool, str]:
    import numpy as np
    from cv_bridge import CvBridge

    bridge = CvBridge()
    before = np.arange(18, dtype=np.uint8).reshape((2, 3, 3))
    message = bridge.cv2_to_imgmsg(before, encoding="rgb8")
    after = bridge.imgmsg_to_cv2(message, desired_encoding="rgb8")
    passed = bool(np.array_equal(before, after))
    return passed, "pixel-identical round trip" if passed else "pixel data changed"


def _check_platform(context: ProbeContext, profile: Mapping[str, object]) -> list[CheckResult]:
    os_profile = _mapping(profile, "os")
    expected_id = _string(os_profile, "id")
    expected_version = _string(os_profile, "version_id")
    actual_id = context.os_release.get("ID", "unknown")
    actual_version = context.os_release.get("VERSION_ID", "unknown")
    os_ok = actual_id == expected_id and actual_version == expected_version

    expected_arch = _string(profile, "architecture")
    arch_ok = context.machine == expected_arch
    return [
        CheckResult(
            check_id="host.os",
            status=PASS if os_ok else FAIL,
            summary="Supported operating system",
            expected=f"{expected_id} {expected_version}",
            observed=f"{actual_id} {actual_version}",
            detail="The first profile follows the ROS 2 Jazzy binary platform target.",
            remediation=(
                "Use the qualified Ubuntu 24.04 host; do not mix Jazzy binary packages "
                "with this host ABI."
                if not os_ok
                else ""
            ),
        ),
        CheckResult(
            check_id="host.architecture",
            status=PASS if arch_ok else FAIL,
            summary="Supported CPU architecture",
            expected=expected_arch,
            observed=context.machine or "unknown",
            remediation=(
                f"Use a {expected_arch} host for the first qualification." if not arch_ok else ""
            ),
        ),
    ]


def _check_python(context: ProbeContext, profile: Mapping[str, object]) -> list[CheckResult]:
    python_profile = _mapping(profile, "python")
    expected_major = _integer(python_profile, "major")
    expected_minor = _integer(python_profile, "minor")
    expected_executable = _string(python_profile, "executable")
    actual_version = ".".join(str(part) for part in context.python_version)
    version_ok = context.python_version[:2] == (expected_major, expected_minor)
    executable_ok = Path(context.python_base_executable).resolve() == Path(
        expected_executable
    ).resolve()
    executable_observation = context.python_executable
    if context.python_base_executable != context.python_executable:
        executable_observation += f" (base: {context.python_base_executable})"
    return [
        CheckResult(
            check_id="python.version",
            status=PASS if version_ok else FAIL,
            summary="ROS-compatible Python ABI",
            expected=f"{expected_major}.{expected_minor}.x",
            observed=actual_version,
            detail="Binary rclpy and cv_bridge must use the qualified system Python ABI.",
            remediation=(
                f"Run with {expected_executable} from the supported host; do not shadow ROS "
                "packages with incompatible wheels."
                if not version_ok
                else ""
            ),
        ),
        CheckResult(
            check_id="python.executable",
            status=PASS if executable_ok else FAIL,
            summary="System interpreter ancestry",
            expected=f"directly use or derive from {expected_executable}",
            observed=executable_observation,
            remediation=(
                f"Create the project environment from {expected_executable} after installing "
                "the supported system environment."
                if not executable_ok
                else ""
            ),
        ),
    ]


def _check_commands(context: ProbeContext, profile: Mapping[str, object]) -> list[CheckResult]:
    results: list[CheckResult] = []
    commands = _sequence(profile, "required_commands")
    for command_value in commands:
        command = str(command_value)
        location = context.which(command)
        results.append(
            CheckResult(
                check_id=f"command.{command}",
                status=PASS if location else MISSING,
                summary=f"Required command: {command}",
                expected="available on PATH",
                observed=location or "not found",
                remediation=(
                    f"Install {command} using the documented supported-host prerequisite step."
                    if not location
                    else ""
                ),
            )
        )
    return results


def _module_version(
    context: ProbeContext, distribution: str, module: str
) -> tuple[str, str]:
    spec = context.find_spec(module)
    if spec is None:
        return "", ""
    try:
        version = context.distribution_version(distribution)
    except importlib.metadata.PackageNotFoundError:
        version = "unknown distribution version"
    origin = getattr(spec, "origin", None) or "namespace/built-in"
    return version, str(origin)


def _check_imports(context: ProbeContext, profile: Mapping[str, object]) -> list[CheckResult]:
    results: list[CheckResult] = []
    imports = _sequence(profile, "required_imports")
    for entry_value in imports:
        if not isinstance(entry_value, dict):
            raise ValueError("required_imports entries must be objects")
        module = _string(entry_value, "module")
        distribution = _string(entry_value, "distribution")
        ownership = entry_value.get("ownership", "any")
        if ownership not in {"any", "ros_or_os"}:
            raise ValueError(f"unsupported ownership for {module}: {ownership}")
        version, origin = _module_version(context, distribution, module)
        present = bool(origin)
        source_ok = ownership != "ros_or_os" or _is_ros_or_os_path(origin)
        status = PASS if present and source_ok else FAIL if present else MISSING
        if present and not source_ok:
            remediation = (
                f"Remove the shadowing {module} package and use the qualified ROS/OS build."
            )
        elif not present:
            remediation = (
                f"Install the qualified {distribution} package without replacing ROS-owned "
                "Python packages."
            )
        else:
            remediation = ""
        results.append(
            CheckResult(
                check_id=f"import.{module}",
                status=status,
                summary=f"Required Python import: {module}",
                expected=(
                    f"importable from ROS/OS ({distribution})"
                    if ownership == "ros_or_os"
                    else f"importable ({distribution})"
                ),
                observed=f"{version} at {origin}" if present else "not importable",
                remediation=remediation,
            )
        )
    return results


def _is_ros_or_os_path(origin: str) -> bool:
    normalized = origin.replace("\\", "/")
    return normalized.startswith("/opt/ros/") or normalized.startswith("/usr/lib/python3/")


def _check_ros_environment(
    context: ProbeContext, profile: Mapping[str, object]
) -> list[CheckResult]:
    ros_profile = _mapping(profile, "ros")
    expected_distro = _string(ros_profile, "distro")
    actual_distro = context.environ.get("ROS_DISTRO", "not sourced")
    distro_ok = actual_distro == expected_distro
    results = [
        CheckResult(
            check_id="ros.environment",
            status=PASS if distro_ok else MISSING,
            summary="ROS environment",
            expected=f"ROS_DISTRO={expected_distro}",
            observed=f"ROS_DISTRO={actual_distro}",
            remediation=(
                f"Install ROS 2 {expected_distro.title()} on the supported host and source its "
                "setup file."
                if not distro_ok
                else ""
            ),
        )
    ]

    ros2 = context.which("ros2")
    for package_value in _sequence(ros_profile, "required_packages"):
        package = str(package_value)
        if ros2 is None:
            results.append(
                CheckResult(
                    check_id=f"ros.package.{package}",
                    status=BLOCKED,
                    summary=f"Required ROS package: {package}",
                    expected="discoverable through ros2 pkg prefix",
                    observed="ros2 command unavailable",
                    remediation="Restore the supported, sourced ROS environment first.",
                )
            )
            continue
        try:
            completed = context.run((ros2, "pkg", "prefix", package))
        except (OSError, subprocess.SubprocessError) as error:
            results.append(
                CheckResult(
                    check_id=f"ros.package.{package}",
                    status=ERROR,
                    summary=f"Required ROS package: {package}",
                    expected="discoverable through ros2 pkg prefix",
                    observed=type(error).__name__,
                    detail=str(error),
                    remediation="Fix the ROS CLI environment, then rerun doctor.",
                )
            )
            continue
        output = completed.stdout.strip()
        found = completed.returncode == 0 and bool(output)
        detail = completed.stderr.strip()
        results.append(
            CheckResult(
                check_id=f"ros.package.{package}",
                status=PASS if found else MISSING,
                summary=f"Required ROS package: {package}",
                expected="discoverable through ros2 pkg prefix",
                observed=output if found else "not found",
                detail=detail,
                remediation=(
                    f"Install and source the qualified {package} package." if not found else ""
                ),
            )
        )
    return results


def _check_image_conversion(context: ProbeContext) -> CheckResult:
    prerequisites = ("rclpy", "cv_bridge", "sensor_msgs", "numpy")
    missing = [module for module in prerequisites if context.find_spec(module) is None]
    if missing:
        return CheckResult(
            check_id="ros.image_conversion",
            status=BLOCKED,
            summary="ROS Image/cv_bridge round trip",
            expected="2x3 RGB image survives a cv_bridge round trip",
            observed=f"blocked by missing imports: {', '.join(missing)}",
            remediation="Install the qualified ROS image stack, then rerun doctor.",
        )

    try:
        passed, observed = context.image_roundtrip()
    except Exception as error:  # external ABI/import errors are evidence, not crashes
        return CheckResult(
            check_id="ros.image_conversion",
            status=FAIL,
            summary="ROS Image/cv_bridge round trip",
            expected="2x3 RGB image survives a cv_bridge round trip",
            observed=f"{type(error).__name__}: {error}",
            remediation="Resolve the ROS/OpenCV/NumPy ABI conflict; do not shadow OS packages.",
        )
    return CheckResult(
        check_id="ros.image_conversion",
        status=PASS if passed else FAIL,
        summary="ROS Image/cv_bridge round trip",
        expected="2x3 RGB image survives a cv_bridge round trip",
        observed=observed,
        remediation="Resolve the image conversion mismatch." if not passed else "",
    )


def _check_version_selection(profile: Mapping[str, object]) -> CheckResult:
    selection = _mapping(profile, "selection")
    qualification = selection.get("qualification")
    unresolved = sorted(
        key for key, value in selection.items() if key != "qualification" and value is None
    )
    passed = qualification == "qualified" and not unresolved
    observations = [f"qualification={qualification or 'missing'}"]
    if unresolved:
        observations.append(f"unresolved={','.join(unresolved)}")
    return CheckResult(
        check_id="profile.version_lock",
        status=PASS if passed else BLOCKED,
        summary="Exact dependency selection",
        expected="qualified profile with no unresolved version fields",
        observed="; ".join(observations),
        detail="A set of present imports is not a qualified ROS/controller/SDK combination.",
        remediation=(
            "Complete the functional substrate spikes, record exact versions/commits, then mark "
            "the compatibility profile qualified."
            if not passed
            else ""
        ),
    )


def _mapping(value: Mapping[str, object], key: str) -> Mapping[str, object]:
    nested = value.get(key)
    if not isinstance(nested, dict):
        raise ValueError(f"{key} must be an object")
    return nested


def _sequence(value: Mapping[str, object], key: str) -> Sequence[object]:
    nested = value.get(key)
    if not isinstance(nested, list):
        raise ValueError(f"{key} must be an array")
    return nested


def _string(value: Mapping[str, object], key: str) -> str:
    nested = value.get(key)
    if not isinstance(nested, str) or not nested:
        raise ValueError(f"{key} must be a non-empty string")
    return nested


def _integer(value: Mapping[str, object], key: str) -> int:
    nested = value.get(key)
    if not isinstance(nested, int):
        raise ValueError(f"{key} must be an integer")
    return nested


def load_profile(path: Path) -> tuple[str, Mapping[str, object]]:
    try:
        contents = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot load compatibility profile {path}: {error}") from error
    if not isinstance(contents, dict):
        raise ValueError("compatibility profile must be an object")
    profile_id = _string(contents, "profile_id")
    return profile_id, contents


def run_doctor(
    profile_path: Path,
    *,
    context: ProbeContext | None = None,
    now: datetime | None = None,
) -> DoctorReport:
    """Run all currently automated substrate checks without mutating the host."""

    profile_id, profile = load_profile(profile_path)
    context = context or ProbeContext.live()
    generated = now or datetime.now(UTC)
    checks = tuple(
        [
            *_check_platform(context, profile),
            *_check_python(context, profile),
            *_check_commands(context, profile),
            *_check_imports(context, profile),
            *_check_ros_environment(context, profile),
            _check_image_conversion(context),
            _check_version_selection(profile),
        ]
    )
    counts = Counter(check.status for check in checks)
    unhealthy = any(check.required and check.status in UNHEALTHY_STATUSES for check in checks)
    stamp = generated.astimezone(UTC).isoformat().replace("+00:00", "Z")
    runtime_id = f"doctor-{generated.astimezone(UTC).strftime('%Y%m%dT%H%M%S.%fZ')}"
    return DoctorReport(
        schema_version=1,
        profile_id=profile_id,
        generated_at=stamp,
        runtime_id=runtime_id,
        overall="red" if unhealthy else "green",
        counts=dict(counts),
        checks=checks,
    )


def write_report(report: DoctorReport, path: Path) -> DoctorReport:
    path.parent.mkdir(parents=True, exist_ok=True)
    recorded = replace(report, report_path=str(path))
    with path.open("x", encoding="utf-8") as stream:
        json.dump(recorded.to_dict(), stream, indent=2, sort_keys=True)
        stream.write("\n")
    return recorded


def default_report_path(project_root: Path, report: DoctorReport) -> Path:
    return project_root / "runs" / "doctor" / report.runtime_id / "manifest.json"
