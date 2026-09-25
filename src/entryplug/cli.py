"""Command-line entry point for the Entryplug implementation."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from entryplug.doctor import DoctorReport, default_report_path, run_doctor, write_report
from entryplug.pilot import run_mirrors_pilot
from entryplug.runtime import DEFAULT_HARBOR_IMAGE, run_harbor, supported_cases


def source_root() -> Path | None:
    candidate = Path(__file__).resolve().parents[2]
    if (candidate / "pyproject.toml").is_file() and (candidate / "deps").is_dir():
        return candidate
    return None


def compatibility_profile() -> Path:
    root = source_root()
    if root is not None:
        source_profile = root / "deps" / "compatibility.json"
        if source_profile.is_file():
            return source_profile

    try:
        distribution = importlib.metadata.distribution("entryplug")
    except importlib.metadata.PackageNotFoundError as error:
        raise FileNotFoundError(
            "deps/compatibility.json is unavailable in the source tree or installation"
        ) from error
    suffix = "share/entryplug/deps/compatibility.json"
    for relative in distribution.files or ():
        if str(relative).replace("\\", "/").endswith(suffix):
            installed_profile = Path(str(distribution.locate_file(relative)))
            if installed_profile.is_file():
                return installed_profile
    raise FileNotFoundError("installed Entryplug distribution has no compatibility profile")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="entryplug")
    parser.add_argument("--version", action="version", version="entryplug 0.0.1")
    commands = parser.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser("doctor", help="record read-only substrate qualification checks")
    doctor.add_argument("--json", action="store_true", help="emit the complete report as JSON")
    doctor.add_argument(
        "--profile",
        default="full",
        help="readiness profile to evaluate (default: full)",
    )
    doctor.add_argument(
        "--output",
        type=Path,
        help="write evidence to this new file (default: runs/doctor/<runtime>/manifest.json)",
    )
    doctor.add_argument(
        "--no-record",
        action="store_true",
        help="do not write a report file (useful for ephemeral checks)",
    )

    commands.add_parser("test", help="run ROS-independent tests with the selected Python")

    up = commands.add_parser("up", help="run an isolated capability qualification case")
    up.add_argument("--runtime", choices=("container",), required=True)
    up.add_argument("--case", choices=supported_cases(), required=True)
    up.add_argument("--image", default=DEFAULT_HARBOR_IMAGE)
    up.add_argument(
        "--reuse-from",
        metavar="RUN_ID",
        help="recheck a prior mirrors run's cached binding before reacquiring",
    )
    up.add_argument(
        "--seed",
        type=int,
        help="set deterministic Hall of Mirrors random streams",
    )
    up.add_argument(
        "--build",
        action="store_true",
        help="explicitly build the pinned runtime image before starting",
    )

    pilot = commands.add_parser("pilot", help="run a declared Hall of Mirrors seed suite")
    pilot.add_argument("--runtime", choices=("container",), required=True)
    pilot.add_argument("--phase", choices=("development", "holdout"), required=True)
    pilot.add_argument("--image", default=DEFAULT_HARBOR_IMAGE)
    pilot.add_argument(
        "--build",
        action="store_true",
        help="explicitly build the pinned runtime image before the first episode",
    )

    replay = commands.add_parser(
        "openenv-replay",
        help="replay qualified Hall of Mirrors evidence through OpenEnv",
    )
    replay.add_argument(
        "--source-run",
        required=True,
        metavar="RUN_ID",
        help="passed Harbor mirrors run whose opaque evidence will be replayed",
    )

    live_openenv = commands.add_parser(
        "openenv-harbor",
        help="run one live Harbor acquisition through OpenEnv",
    )
    live_openenv.add_argument("--runtime", choices=("container",), required=True)
    live_openenv.add_argument("--seed", type=int, required=True)
    live_openenv.add_argument("--image", default=DEFAULT_HARBOR_IMAGE)

    live_a2a = commands.add_parser(
        "a2a-harbor",
        help="run one live Harbor acquisition through A2A",
    )
    live_a2a.add_argument("--runtime", choices=("container",), required=True)
    live_a2a.add_argument("--seed", type=int, required=True)
    live_a2a.add_argument("--image", default=DEFAULT_HARBOR_IMAGE)

    live_mcp = commands.add_parser(
        "mcp-harbor",
        help="run one live Harbor acquisition through MCP",
    )
    live_mcp.add_argument("--runtime", choices=("container",), required=True)
    live_mcp.add_argument("--seed", type=int, required=True)
    live_mcp.add_argument("--image", default=DEFAULT_HARBOR_IMAGE)
    return parser


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    raw = list(argv) if argv is not None else sys.argv[1:]
    if raw and raw[0] == "test" and not (len(raw) == 2 and raw[1] in {"-h", "--help"}):
        forwarded = raw[1:]
        if forwarded[:1] == ["--"]:
            forwarded = forwarded[1:]
        suite = "core"
        if forwarded[:1] == ["--suite"]:
            if len(forwarded) < 2:
                raise SystemExit("entryplug test: --suite requires a value")
            suite = forwarded[1]
            forwarded = forwarded[2:]
        elif forwarded[:1] and forwarded[0].startswith("--suite="):
            suite = forwarded.pop(0).split("=", 1)[1]
        if suite not in {"core", "a2a", "mcp", "openenv", "all"}:
            raise SystemExit(f"entryplug test: unknown suite {suite!r}")
        return argparse.Namespace(command="test", pytest_args=forwarded, test_suite=suite)

    parser = _parser()
    return parser.parse_args(raw)


def _render_text(report: DoctorReport) -> str:
    marks = {
        "pass": "PASS",
        "fail": "FAIL",
        "missing": "MISS",
        "blocked": "BLOCK",
        "error": "ERROR",
        "out_of_scope": "N/A",
    }
    lines = [
        (
            f"entryplug doctor: {report.overall.upper()} "
            f"({report.profile_id}, readiness={report.readiness_profile})"
        ),
        f"runtime: {report.runtime_id}",
        "",
    ]
    for check in report.checks:
        mark = marks.get(check.status, check.status.upper())
        lines.append(f"[{mark:5}] {check.check_id}: {check.observed}")
        if check.status != "pass" and check.remediation:
            lines.append(f"        -> {check.remediation}")
    if report.report_path:
        lines.extend(("", f"evidence: {report.report_path}"))
    if report.overall != "green":
        lines.extend(
            (
                "",
                "The substrate is not qualified. No simulator capability is claimed.",
            )
        )
    return "\n".join(lines)


def _doctor(args: argparse.Namespace) -> int:
    root = source_root() or Path.cwd()
    try:
        report = run_doctor(compatibility_profile(), readiness_profile=args.profile)
        if not args.no_record:
            output = args.output or default_report_path(root, report)
            report = write_report(report, output)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"entryplug doctor: {error}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        print(_render_text(report))
    return 0 if report.overall == "green" else 1


def _test(args: argparse.Namespace) -> int:
    root = source_root()
    if root is None:
        print("entryplug test: run this command from a source checkout", file=sys.stderr)
        return 2
    suite = getattr(args, "test_suite", "core")
    targets: list[str] = []
    if suite in {"a2a", "mcp", "openenv"}:
        targets.append(str(root / "optional_tests" / suite))
    elif suite == "all":
        targets.extend(
            (
                str(root / "tests"),
                str(root / "optional_tests" / "a2a"),
                str(root / "optional_tests" / "mcp"),
                str(root / "optional_tests" / "openenv"),
            )
        )
    command = [sys.executable, "-m", "pytest", *args.pytest_args, *targets]
    environment = dict(os.environ)
    source = str(root / "src")
    environment["PYTHONPATH"] = source + (
        os.pathsep + environment["PYTHONPATH"] if environment.get("PYTHONPATH") else ""
    )
    return subprocess.run(command, cwd=root, env=environment, check=False).returncode


def _up(args: argparse.Namespace) -> int:
    root = source_root()
    if root is None:
        print("entryplug up: run this command from a source checkout", file=sys.stderr)
        return 2
    try:
        result = run_harbor(
            root,
            image=args.image,
            case=args.case,
            reuse_from=args.reuse_from,
            seed=args.seed,
            build=args.build,
        )
    except (OSError, RuntimeError, ValueError) as error:
        print(f"entryplug up: {error}", file=sys.stderr)
        return 2
    if result.evidence_path.is_dir():
        print(f"evidence: {result.evidence_path}")
    else:
        print("entryplug up: no evidence was produced", file=sys.stderr)
    return result.returncode


def _pilot(args: argparse.Namespace) -> int:
    root = source_root()
    if root is None:
        print("entryplug pilot: run this command from a source checkout", file=sys.stderr)
        return 2
    try:
        result = run_mirrors_pilot(
            root,
            phase=args.phase,
            image=args.image,
            build=args.build,
        )
    except (OSError, RuntimeError, ValueError) as error:
        print(f"entryplug pilot: {error}", file=sys.stderr)
        return 2
    print(
        f"mirror pilot: {result.passed_count}/{result.episode_count} passed\n"
        f"evidence: {result.evidence_path}"
    )
    return 0 if result.passed else 1


def _openenv_replay(args: argparse.Namespace) -> int:
    root = source_root()
    if root is None:
        print("entryplug openenv-replay: run this command from a source checkout", file=sys.stderr)
        return 2
    try:
        from entryplug_openenv.mirror_replay import run_openenv_mirror_replay
    except ImportError:
        print(
            "entryplug openenv-replay: install the exact optional dependency with "
            "pip install -e '.[openenv]'",
            file=sys.stderr,
        )
        return 2
    try:
        result = run_openenv_mirror_replay(root, args.source_run)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"entryplug openenv-replay: {error}", file=sys.stderr)
        return 2
    print(
        f"OpenEnv mirror replay: {'passed' if result.passed else 'failed'}\n"
        f"evidence: {result.evidence_path}"
    )
    return 0 if result.passed else 1


def _openenv_harbor(args: argparse.Namespace) -> int:
    root = source_root()
    if root is None:
        print("entryplug openenv-harbor: run this command from a source checkout", file=sys.stderr)
        return 2
    try:
        from entryplug_openenv.live_harbor import run_openenv_harbor
    except ImportError:
        print(
            "entryplug openenv-harbor: install the exact optional dependency with "
            "pip install -e '.[openenv]'",
            file=sys.stderr,
        )
        return 2
    try:
        result = run_openenv_harbor(
            root,
            seed=args.seed,
            image=args.image,
        )
    except (OSError, RuntimeError, ValueError) as error:
        print(f"entryplug openenv-harbor: {error}", file=sys.stderr)
        return 2
    print(
        f"OpenEnv Harbor episode: {'passed' if result.passed else 'failed'}\n"
        f"physical evidence: {result.physical_evidence_path}\n"
        f"wrapper evidence: {result.evidence_path}"
    )
    return 0 if result.passed else 1


def _a2a_harbor(args: argparse.Namespace) -> int:
    root = source_root()
    if root is None:
        print(
            "entryplug a2a-harbor: run this command from a source checkout",
            file=sys.stderr,
        )
        return 2
    try:
        from entryplug_a2a.live_harbor import run_a2a_harbor
    except ImportError:
        print(
            "entryplug a2a-harbor: install the exact optional dependency with "
            "pip install -e '.[a2a]'",
            file=sys.stderr,
        )
        return 2
    try:
        result = run_a2a_harbor(
            root,
            seed=args.seed,
            image=args.image,
        )
    except (OSError, RuntimeError, ValueError) as error:
        print(f"entryplug a2a-harbor: {error}", file=sys.stderr)
        return 2
    print(
        f"A2A Harbor episode: {'passed' if result.passed else 'failed'}\n"
        f"physical evidence: {result.physical_evidence_path}\n"
        f"wrapper evidence: {result.evidence_path}"
    )
    return 0 if result.passed else 1


def _mcp_harbor(args: argparse.Namespace) -> int:
    root = source_root()
    if root is None:
        print(
            "entryplug mcp-harbor: run this command from a source checkout",
            file=sys.stderr,
        )
        return 2
    try:
        from entryplug_mcp.live_harbor import run_mcp_harbor
    except ImportError:
        print(
            "entryplug mcp-harbor: install the exact optional dependency with "
            "pip install -e '.[mcp]'",
            file=sys.stderr,
        )
        return 2
    try:
        result = run_mcp_harbor(
            root,
            seed=args.seed,
            image=args.image,
        )
    except (OSError, RuntimeError, ValueError) as error:
        print(f"entryplug mcp-harbor: {error}", file=sys.stderr)
        return 2
    print(
        f"MCP Harbor episode: {'passed' if result.passed else 'failed'}\n"
        f"physical evidence: {result.physical_evidence_path}\n"
        f"wrapper evidence: {result.evidence_path}"
    )
    return 0 if result.passed else 1


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.command == "doctor":
        return _doctor(args)
    if args.command == "test":
        return _test(args)
    if args.command == "up":
        return _up(args)
    if args.command == "pilot":
        return _pilot(args)
    if args.command == "openenv-replay":
        return _openenv_replay(args)
    if args.command == "openenv-harbor":
        return _openenv_harbor(args)
    if args.command == "a2a-harbor":
        return _a2a_harbor(args)
    if args.command == "mcp-harbor":
        return _mcp_harbor(args)
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
