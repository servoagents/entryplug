"""Owned Harbor process and task level episode capability."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol

from entryplug.episode import EpisodeFactory, EpisodeStart
from entryplug.evidence import JsonValue
from entryplug.operation import (
    CapabilitySpec,
    Lifecycle,
    MotionState,
    OperationContext,
    OperationHost,
    OperationResult,
)
from entryplug.qualification import HARBOR_MIRRORS_V1
from entryplug.runtime import (
    DEFAULT_HARBOR_IMAGE,
    harbor_container_name,
    harbor_run_command,
    new_harbor_run_id,
)
from entryplug.seeding import validate_experiment_seed
from entryplug.session import Session

ACQUIRE_VISUAL_BINDING = "acquire_visual_binding"


@dataclass(frozen=True, slots=True)
class StopReport:
    """Whether an owned container was confirmed stopped and how."""

    confirmed: bool
    forced: bool


class HarborRun(Protocol):
    """Minimum owned process surface required by the operation handler."""

    run_id: str
    evidence_path: Path

    async def wait(self) -> int: ...

    async def stop(self) -> StopReport: ...


StartHarbor = Callable[[Path, str, str, int], Awaitable[HarborRun]]
ImageCheck = Callable[[str], Awaitable[bool]]


class DockerHarborRun:
    """One attached Docker run whose container name remains under our control."""

    def __init__(
        self,
        *,
        run_id: str,
        evidence_path: Path,
        container_name: str,
        process: asyncio.subprocess.Process,
        temporary_log_path: Path,
        temporary_log: BinaryIO,
    ) -> None:
        self.run_id = run_id
        self.evidence_path = evidence_path
        self.container_name = container_name
        self._process = process
        self._temporary_log_path = temporary_log_path
        self._temporary_log = temporary_log
        self._wait_lock = asyncio.Lock()
        self._finalized = False

    def _finalize(self) -> None:
        if self._finalized:
            return
        self._temporary_log.flush()
        self._temporary_log.close()
        self.evidence_path.mkdir(parents=True, exist_ok=True)
        launcher_log = self.evidence_path / "launcher.log"
        with launcher_log.open("xb") as output, self._temporary_log_path.open("rb") as source:
            shutil.copyfileobj(source, output)
        self._temporary_log_path.unlink()
        self._finalized = True

    async def wait(self) -> int:
        async with self._wait_lock:
            returncode = await self._process.wait()
            self._finalize()
            return returncode

    async def _docker_command(self, *arguments: str, timeout_seconds: float) -> int | None:
        process = await asyncio.create_subprocess_exec(
            "docker",
            *arguments,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            async with asyncio.timeout(timeout_seconds):
                return await process.wait()
        except asyncio.CancelledError:
            process.kill()
            await process.wait()
            raise
        except TimeoutError:
            process.kill()
            await process.wait()
            return None

    async def _wait_after_stop(self, timeout_seconds: float) -> bool:
        try:
            async with asyncio.timeout(timeout_seconds):
                await self.wait()
            return True
        except TimeoutError:
            return False

    async def stop(self) -> StopReport:
        if self._process.returncode is not None:
            await self.wait()
            return StopReport(True, False)

        stopped = await self._docker_command(
            "stop",
            "--timeout",
            "3",
            self.container_name,
            timeout_seconds=7.0,
        )
        if stopped == 0 and await self._wait_after_stop(5.0):
            return StopReport(True, False)
        if self._process.returncode is not None:
            await self.wait()
            return StopReport(True, False)

        killed = await self._docker_command(
            "kill",
            self.container_name,
            timeout_seconds=4.0,
        )
        if killed == 0 and await self._wait_after_stop(5.0):
            return StopReport(True, True)
        if self._process.returncode is not None:
            await self.wait()
            return StopReport(True, True)
        return StopReport(False, killed == 0)


async def docker_image_available(image: str) -> bool:
    """Check the selected image without mutating Docker state."""

    process = await asyncio.create_subprocess_exec(
        "docker",
        "image",
        "inspect",
        image,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return await process.wait() == 0


async def start_owned_harbor(
    root: Path,
    image: str,
    run_id: str,
    seed: int,
) -> DockerHarborRun:
    """Launch the existing mirrors command with a bounded, capture-safe log."""

    runs = root / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.NamedTemporaryFile(
        mode="w+b",
        prefix=f"entryplug-{run_id}-",
        suffix=".log",
        dir="/tmp",
        delete=False,
    )
    temporary_path = Path(temporary.name)
    try:
        process = await asyncio.create_subprocess_exec(
            *harbor_run_command(root, image, run_id, "mirrors", seed=seed),
            cwd=root,
            stdout=temporary,
            stderr=subprocess.STDOUT,
        )
    except Exception:
        temporary.close()
        temporary_path.unlink(missing_ok=True)
        raise
    return DockerHarborRun(
        run_id=run_id,
        evidence_path=runs / run_id,
        container_name=harbor_container_name(run_id),
        process=process,
        temporary_log_path=temporary_path,
        temporary_log=temporary,
    )


def _no_arguments(arguments: Mapping[str, JsonValue]) -> Mapping[str, object]:
    if arguments:
        raise ValueError("visual binding acquisition takes no arguments")
    return {}


def _public_result(run: HarborRun) -> dict[str, object]:
    summary_path = run.evidence_path / "mirrors.json"
    value = json.loads(summary_path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping) or value.get("status") != "passed":
        raise ValueError("Harbor mirrors evidence did not report a passed result")
    association = value.get("association")
    selected = association.get("candidate_id") if isinstance(association, Mapping) else None
    if not isinstance(selected, str) or not selected:
        raise ValueError("Harbor mirrors evidence has no public association selection")
    qualification = value.get("qualification_profile")
    if not isinstance(qualification, Mapping):
        raise ValueError("Harbor mirrors evidence has no qualification profile")
    if (
        qualification.get("profile_id") != HARBOR_MIRRORS_V1.profile_id
        or qualification.get("digest") != HARBOR_MIRRORS_V1.digest
    ):
        raise ValueError("Harbor mirrors evidence used a different qualification contract")
    return {
        "run_id": run.run_id,
        "evidence_path": str(run.evidence_path),
        "status": "passed",
        "selected_candidate_id": selected,
        "qualification_profile": HARBOR_MIRRORS_V1.profile_id,
        "qualification_profile_digest": HARBOR_MIRRORS_V1.digest,
    }


def _stopped_result(
    run: HarborRun,
    stopped: StopReport,
    *,
    reason_code: str,
) -> OperationResult:
    if stopped.confirmed:
        return OperationResult(
            Lifecycle.CANCELED,
            MotionState.IDLE,
            {
                "run_id": run.run_id,
                "evidence_path": str(run.evidence_path),
                "forced_stop": stopped.forced,
                "quiescence_confirmed": True,
            },
            reason_code=reason_code,
        )
    return OperationResult(
        Lifecycle.INDETERMINATE,
        MotionState.UNKNOWN,
        {
            "run_id": run.run_id,
            "evidence_path": str(run.evidence_path),
            "container_name": harbor_container_name(run.run_id),
            "forced_stop": stopped.forced,
            "quiescence_confirmed": False,
        },
        reason_code="STOP_UNCONFIRMED",
    )


def harbor_mirrors_episode_factory(
    root: Path,
    *,
    image: str = DEFAULT_HARBOR_IMAGE,
    start_harbor: StartHarbor = start_owned_harbor,
    image_check: ImageCheck = docker_image_available,
) -> EpisodeFactory:
    """Create resettable episodes that own at most one mirrors container."""

    async def factory(seed: int) -> EpisodeStart:
        validate_experiment_seed(seed)
        if not await image_check(image):
            raise FileNotFoundError(
                f"container image {image!r} is unavailable; build it before reset"
            )

        async def acquire(
            context: OperationContext,
            _: Mapping[str, JsonValue],
        ) -> OperationResult:
            if context.cancel_requested:
                return OperationResult(Lifecycle.CANCELED, MotionState.IDLE)
            run_id = new_harbor_run_id("mirrors", seed)
            run = await start_harbor(root, image, run_id, seed)
            context.report("harbor_running", MotionState.MOVING)
            wait_task = asyncio.create_task(run.wait())
            cancel_task = asyncio.create_task(context.wait_for_cancel())
            try:
                done, _ = await asyncio.wait(
                    (wait_task, cancel_task),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if wait_task in done:
                    returncode = wait_task.result()
                    if returncode != 0:
                        return OperationResult(
                            Lifecycle.FAILED,
                            MotionState.IDLE,
                            {
                                "run_id": run.run_id,
                                "evidence_path": str(run.evidence_path),
                                "returncode": returncode,
                            },
                            reason_code="HARBOR_CASE_FAILED",
                        )
                    try:
                        result = _public_result(run)
                    except (OSError, ValueError, json.JSONDecodeError) as error:
                        return OperationResult(
                            Lifecycle.FAILED,
                            MotionState.IDLE,
                            {
                                "run_id": run.run_id,
                                "evidence_path": str(run.evidence_path),
                                "error_type": type(error).__name__,
                            },
                            reason_code="INVALID_HARBOR_EVIDENCE",
                        )
                    return OperationResult(Lifecycle.SUCCEEDED, MotionState.IDLE, result)

                context.report("harbor_stopping", MotionState.MOVING)
                stopped = await run.stop()
                return _stopped_result(
                    run,
                    stopped,
                    reason_code="CANCEL_REQUESTED",
                )
            except asyncio.CancelledError:
                context.report("harbor_stopping", MotionState.MOVING)
                stopped = await run.stop()
                return _stopped_result(
                    run,
                    stopped,
                    reason_code="WORKER_CANCELED",
                )
            finally:
                cancel_task.cancel()
                if not wait_task.done():
                    wait_task.cancel()

        host = OperationHost(
            (
                CapabilitySpec(
                    ACQUIRE_VISUAL_BINDING,
                    "1",
                    "Acquire and validate one usable visual binding in Harbor",
                    True,
                    180.0,
                    20.0,
                    _no_arguments,
                    acquire,
                ),
            ),
            runtime_id=f"harbor-mirrors-episode-{seed}-{uuid.uuid4().hex}",
        )
        return EpisodeStart(
            Session(host, owns_runtime=True),
            {
                "fixture": "harbor_mirrors_live",
                "image": image,
                "qualification_profile": HARBOR_MIRRORS_V1.profile_id,
                "qualification_profile_digest": HARBOR_MIRRORS_V1.digest,
                "reset_mode": "new_owned_container_per_acquisition",
            },
        )

    return factory
