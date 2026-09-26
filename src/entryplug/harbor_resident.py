"""One owned Harbor world serving sequential admitted visual tasks."""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import BinaryIO, Protocol, cast

from entryplug.episode import EpisodeFactory, EpisodeStart
from entryplug.evidence import JsonValue, json_object
from entryplug.harbor_episode import DockerHarborRun, ImageCheck, StopReport, docker_image_available
from entryplug.operation import (
    AdmissionError,
    CapabilitySpec,
    Lifecycle,
    MotionState,
    OperationContext,
    OperationHost,
    OperationResult,
    OperationSnapshot,
)
from entryplug.runtime import (
    DEFAULT_HARBOR_IMAGE,
    harbor_container_name,
    harbor_run_command,
    new_harbor_run_id,
)
from entryplug.seeding import validate_experiment_seed
from entryplug.session import Session
from entryplug.visual_task import (
    VISUAL_REACH,
    VISUAL_REACH_INPUT_SCHEMA,
    VISUAL_REACH_RESULT_SCHEMA,
    visual_reach_arguments,
)

MAX_RECORD_BYTES = 16_384
SOURCE_ID = "camera-color-v1"
SOURCE_LINEAGE = "camera-color-capture-v1"


class ResidentReplyLost(RuntimeError):
    """A task reply is absent, so physical state cannot be inferred from it."""


class ResidentRun(Protocol):
    run_id: str
    evidence_path: Path
    ready: Mapping[str, JsonValue]

    async def request(self, operation_id: str, target_y_px: float) -> Mapping[str, JsonValue]: ...

    async def cancel(self, operation_id: str) -> None: ...

    async def stop(self) -> StopReport: ...


class DockerResidentRun(DockerHarborRun):
    """Bounded JSON records over the stdin/stdout of one owned Docker process."""

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
        super().__init__(
            run_id=run_id,
            evidence_path=evidence_path,
            container_name=container_name,
            process=process,
            temporary_log_path=temporary_log_path,
            temporary_log=temporary_log,
        )
        if process.stdin is None or process.stdout is None:
            raise ValueError("resident process requires stdin and stdout pipes")
        self._stdin = process.stdin
        self._stdout = process.stdout
        self._write_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()
        self.ready: Mapping[str, JsonValue] = {}

    async def _read(self, timeout_seconds: float) -> Mapping[str, JsonValue]:
        try:
            async with asyncio.timeout(timeout_seconds):
                line = await self._stdout.readline()
        except (TimeoutError, asyncio.LimitOverrunError) as error:
            raise ResidentReplyLost(
                "resident reply timed out or exceeded the pipe limit"
            ) from error
        if not line or len(line) > MAX_RECORD_BYTES:
            raise ResidentReplyLost("resident reply was absent or oversized")
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise ResidentReplyLost("resident reply was not JSON") from error
        if not isinstance(value, dict) or value.get("version") != 1:
            raise ResidentReplyLost("resident reply did not match the private record version")
        return json_object(value, "resident reply")

    async def _write(self, record: Mapping[str, object]) -> None:
        encoded = json.dumps(record, allow_nan=False, separators=(",", ":")).encode() + b"\n"
        if len(encoded) > MAX_RECORD_BYTES:
            raise ValueError("resident request exceeds the record bound")
        async with self._write_lock:
            try:
                self._stdin.write(encoded)
                async with asyncio.timeout(3.0):
                    await self._stdin.drain()
            except (BrokenPipeError, ConnectionError, TimeoutError) as error:
                raise ResidentReplyLost("resident request pipe is unavailable") from error

    async def initialize(self) -> Mapping[str, JsonValue]:
        record = await self._read(180.0)
        if record.get("type") != "ready" or record.get("run_id") != self.run_id:
            raise ResidentReplyLost("resident did not report a matching ready record")
        self.ready = record
        return record

    async def request(self, operation_id: str, target_y_px: float) -> Mapping[str, JsonValue]:
        async with self._request_lock:
            await self._write(
                {
                    "version": 1,
                    "type": "reach",
                    "operation_id": operation_id,
                    "target_y_px": target_y_px,
                }
            )
            record = await self._read(55.0)
            if record.get("type") != "result" or record.get("operation_id") != operation_id:
                raise ResidentReplyLost("resident returned a mismatched task result")
            return record

    async def cancel(self, operation_id: str) -> None:
        await self._write({"version": 1, "type": "cancel", "operation_id": operation_id})


async def start_owned_resident(root: Path, image: str, run_id: str) -> ResidentRun:
    """Start the world once, with no additional mounts or Docker socket."""

    runs = root / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.NamedTemporaryFile(
        mode="w+b", prefix=f"entryplug-{run_id}-", suffix=".log", dir="/tmp", delete=False
    )
    temporary_path = Path(temporary.name)
    command = harbor_run_command(root, image, run_id, "resident")
    command.insert(3, "-i")
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=root,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=temporary,
        )
    except Exception:
        temporary.close()
        temporary_path.unlink(missing_ok=True)
        raise
    run = DockerResidentRun(
        run_id=run_id,
        evidence_path=runs / run_id,
        container_name=harbor_container_name(run_id),
        process=process,
        temporary_log_path=temporary_path,
        temporary_log=cast(BinaryIO, temporary),
    )
    try:
        await run.initialize()
    except Exception:
        await run.stop()
        raise
    return run


StartResident = Callable[[Path, str, str], Awaitable[ResidentRun]]


class ResidentSession(Session):
    """Closing the owning session closes its world after admitted work settles."""

    def __init__(self, host: OperationHost, run: ResidentRun) -> None:
        super().__init__(host, owns_runtime=True)
        self._run = run
        self._world_closed = False

    async def close(self) -> tuple[OperationSnapshot, ...]:
        snapshots = await super().close()
        if not self._world_closed:
            stopped = await self._run.stop()
            if not stopped.confirmed:
                raise RuntimeError("resident world stop could not be confirmed")
            self._world_closed = True
        return snapshots


def harbor_resident_episode_factory(
    root: Path,
    *,
    image: str = DEFAULT_HARBOR_IMAGE,
    start_resident: StartResident = start_owned_resident,
    image_check: ImageCheck = docker_image_available,
) -> EpisodeFactory:
    """Create an episode whose one world serves sequential visual reach operations."""

    async def factory(seed: int) -> EpisodeStart:
        validate_experiment_seed(seed)
        if not await image_check(image):
            raise FileNotFoundError(
                f"container image {image!r} is unavailable; build it before reset"
            )
        run_id = new_harbor_run_id("resident")
        started = time.monotonic()
        run = await start_resident(root, image, run_id)
        startup_ms = round((time.monotonic() - started) * 1000, 3)
        ready = run.ready
        acquisition_value = ready.get("acquisition_ms")
        container_startup_ms = (
            round(max(0.0, startup_ms - float(acquisition_value)), 3)
            if isinstance(acquisition_value, (int, float))
            and not isinstance(acquisition_value, bool)
            and 0 <= acquisition_value <= startup_ms
            else None
        )
        if (
            ready.get("source_id") != SOURCE_ID
            or ready.get("lineage_id") != SOURCE_LINEAGE
            or not isinstance(ready.get("initial_y_px"), (int, float))
        ):
            await run.stop()
            raise ResidentReplyLost("resident ready record has no supported visual source")

        world_available = True

        def validate_reach(arguments: Mapping[str, JsonValue]) -> Mapping[str, object]:
            if not world_available:
                raise AdmissionError("WORLD_UNAVAILABLE", "resident world is unavailable")
            return visual_reach_arguments(arguments)

        async def reach(
            context: OperationContext, arguments: Mapping[str, JsonValue]
        ) -> OperationResult:
            nonlocal world_available
            target_value = arguments["target_y_px"]
            if isinstance(target_value, bool) or not isinstance(target_value, (int, float)):
                raise ValueError("validated target was not numeric")
            target = float(target_value)
            if context.cancel_requested:
                return OperationResult(Lifecycle.CANCELED, MotionState.IDLE)
            context.report("check_binding_and_reach", MotionState.MOVING)
            reply = asyncio.create_task(run.request(context.operation_id, target))
            canceled = asyncio.create_task(context.wait_for_cancel())
            try:
                done, _ = await asyncio.wait((reply, canceled), return_when=asyncio.FIRST_COMPLETED)
                if canceled in done and not reply.done():
                    await run.cancel(context.operation_id)
                record = await reply
            except (ResidentReplyLost, TimeoutError, OSError, ValueError):
                world_available = False
                context.report("lost_reply_stopping_world", MotionState.UNKNOWN)
                stopped = await run.stop()
                return OperationResult(
                    Lifecycle.FAILED if stopped.confirmed else Lifecycle.INDETERMINATE,
                    MotionState.IDLE if stopped.confirmed else MotionState.UNKNOWN,
                    {"run_id": run.run_id, "quiescence_confirmed": stopped.confirmed},
                    reason_code="REPLY_LOST_STOPPED" if stopped.confirmed else "STOP_UNCONFIRMED",
                )
            finally:
                canceled.cancel()
                if not reply.done():
                    reply.cancel()
            status = record.get("status")
            if status not in {"passed", "failed", "canceled", "refused"}:
                return OperationResult(
                    Lifecycle.INDETERMINATE,
                    MotionState.UNKNOWN,
                    reason_code="INVALID_RESIDENT_RESULT",
                )
            if record.get("source_id") != SOURCE_ID or record.get("lineage_id") != SOURCE_LINEAGE:
                return OperationResult(
                    Lifecycle.INDETERMINATE,
                    MotionState.UNKNOWN,
                    reason_code="SOURCE_IDENTITY_CHANGED",
                )
            if record.get("quiescence_confirmed") is not True:
                return OperationResult(
                    Lifecycle.INDETERMINATE,
                    MotionState.UNKNOWN,
                    reason_code="STOP_UNCONFIRMED",
                )
            context.report("reconciled", MotionState.HOLDING)
            result = {key: value for key, value in record.items() if key not in {"version", "type"}}
            result["run_id"] = run.run_id
            result["evidence_path"] = str(run.evidence_path)
            if status == "passed" and not context.cancel_requested:
                return OperationResult(Lifecycle.SUCCEEDED, MotionState.HOLDING, result)
            if status == "canceled" or context.cancel_requested:
                return OperationResult(
                    Lifecycle.CANCELED, MotionState.HOLDING, result, reason_code="CANCEL_REQUESTED"
                )
            return OperationResult(
                Lifecycle.FAILED,
                MotionState.HOLDING,
                result,
                reason_code="TARGET_REFUSED" if status == "refused" else "TARGET_NOT_REACHED",
            )

        host = OperationHost(
            (
                CapabilitySpec(
                    VISUAL_REACH,
                    "1",
                    "Align a marker to one row in the selected camera view",
                    True,
                    55.0,
                    12.0,
                    validate_reach,
                    reach,
                    VISUAL_REACH_INPUT_SCHEMA,
                    VISUAL_REACH_RESULT_SCHEMA,
                ),
            ),
            runtime_id=f"harbor-resident-{uuid.uuid4().hex}",
        )
        return EpisodeStart(
            ResidentSession(host, run),
            {
                "fixture": "harbor_resident_visual_reach",
                "run_id": run.run_id,
                "source_id": SOURCE_ID,
                "lineage_id": SOURCE_LINEAGE,
                "initial_y_px": ready["initial_y_px"],
                "container_startup_and_acquisition_ms": startup_ms,
                "container_startup_ms": container_startup_ms,
                "acquisition_ms": ready.get("acquisition_ms"),
                "one_world_per_episode": True,
                "target_semantics": "marker centroid at requested image row in selected camera",
            },
        )

    return factory
