"""Explicit episode lifecycle over the same operation host used by free clients."""

from __future__ import annotations

import asyncio
import json
import math
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeVar, cast

from entryplug.agent import (
    AgentRunner,
    AgentStep,
    Decision,
    DecisionResult,
    Stop,
    dispatch_decision,
)
from entryplug.evidence import JsonValue
from entryplug.session import Session


class EpisodeError(RuntimeError):
    """Base error for episode lifecycle violations."""


class EpisodeTimeoutError(EpisodeError):
    """Raised after an episode deadline closes its owned runtime."""


class EpisodeStatus(StrEnum):
    READY = "ready"
    RUNNING = "running"
    STOPPED = "stopped"
    TIMED_OUT = "timed_out"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class EpisodeStart:
    """Fresh owned session plus metadata safe to expose to an agent."""

    session: Session
    public_metadata: Mapping[str, object]


EpisodeFactory = Callable[[int], Awaitable[EpisodeStart]]
ResultT = TypeVar("ResultT")


@dataclass(frozen=True, slots=True)
class EpisodeState:
    """Public episode state with no evaluator truth or private score."""

    episode_id: str
    generation: int
    seed: int
    runtime_id: str
    status: EpisodeStatus
    step_count: int
    public_metadata: Mapping[str, JsonValue]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "episode_id": self.episode_id,
            "generation": self.generation,
            "seed": self.seed,
            "runtime_id": self.runtime_id,
            "status": self.status.value,
            "step_count": self.step_count,
            "public_metadata": cast(JsonValue, dict(self.public_metadata)),
        }


def _public_metadata(value: Mapping[str, object]) -> dict[str, JsonValue]:
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise EpisodeError("public episode metadata must be finite JSON data") from error
    if len(encoded.encode()) > 16_384:
        raise EpisodeError("public episode metadata exceeds 16384 bytes")
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):
        raise EpisodeError("public episode metadata must be an object")
    return cast(dict[str, JsonValue], decoded)


class EpisodeController:
    """Create bounded episodes while leaving policy and operation authority separate."""

    def __init__(self, factory: EpisodeFactory, *, timeout_seconds: float) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("episode timeout must be finite and positive")
        self._factory = factory
        self._timeout_seconds = timeout_seconds
        self._session: Session | None = None
        self._episode_id: str | None = None
        self._generation = 0
        self._seed: int | None = None
        self._runtime_id: str | None = None
        self._status = EpisodeStatus.CLOSED
        self._step_count = 0
        self._metadata: dict[str, JsonValue] = {}
        self._deadline = 0.0
        self._step_in_flight = False
        self._closed = False

    async def reset(self, seed: int) -> EpisodeState:
        """Close the prior owned runtime and create a fresh seeded episode."""

        if self._closed:
            raise EpisodeError("episode controller is closed")
        if self._step_in_flight:
            raise EpisodeError("cannot reset while an agent step is outstanding")
        if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2**63:
            raise ValueError("episode seed must be an integer from 0 through 2^63 - 1")

        previous_runtime_id = self._runtime_id
        if self._session is not None:
            await self._session.close()
            self._session = None
        try:
            start = await self._factory(seed)
            if not start.session.owns_runtime:
                await start.session.close()
                raise EpisodeError("resettable episodes require a runtime owning session")
            view = await start.session.observe()
            if previous_runtime_id is not None and view.runtime_id == previous_runtime_id:
                await start.session.close()
                raise EpisodeError("episode reset must create a new runtime ID")
            metadata = _public_metadata(start.public_metadata)
        except Exception:
            self._status = EpisodeStatus.CLOSED
            raise

        self._session = start.session
        self._episode_id = uuid.uuid4().hex
        self._generation += 1
        self._seed = seed
        self._runtime_id = view.runtime_id
        self._status = EpisodeStatus.READY
        self._step_count = 0
        self._metadata = metadata
        self._deadline = asyncio.get_running_loop().time() + self._timeout_seconds
        return self.observe()

    def observe(self) -> EpisodeState:
        """Return only public lifecycle state for the current episode."""

        if (
            self._session is None
            or self._episode_id is None
            or self._seed is None
            or self._runtime_id is None
        ):
            raise EpisodeError("episode has not been reset")
        return EpisodeState(
            episode_id=self._episode_id,
            generation=self._generation,
            seed=self._seed,
            runtime_id=self._runtime_id,
            status=self._status,
            step_count=self._step_count,
            public_metadata=_public_metadata(self._metadata),
        )

    async def _expire(self) -> None:
        self._status = EpisodeStatus.TIMED_OUT
        if self._session is not None:
            await self._session.close()

    async def _within_deadline(
        self,
        work: Callable[[], Awaitable[ResultT]],
    ) -> ResultT:
        if self._session is None:
            raise EpisodeError("episode has not been reset")
        if self._status in {EpisodeStatus.TIMED_OUT, EpisodeStatus.CLOSED}:
            raise EpisodeError(f"episode is {self._status.value}")
        if self._step_in_flight:
            raise EpisodeError("one episode step is already outstanding")
        remaining = self._deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            await self._expire()
            raise EpisodeTimeoutError("episode deadline expired")

        self._step_in_flight = True
        self._status = EpisodeStatus.RUNNING
        try:
            try:
                async with asyncio.timeout(remaining):
                    return await work()
            except TimeoutError as error:
                await self._expire()
                raise EpisodeTimeoutError("episode deadline expired") from error
        except asyncio.CancelledError:
            if self._status != EpisodeStatus.TIMED_OUT:
                self._status = EpisodeStatus.READY
            raise
        except Exception:
            if self._status != EpisodeStatus.TIMED_OUT:
                self._status = EpisodeStatus.READY
            raise
        finally:
            self._step_in_flight = False

    def _finish_step(self, decision: Decision) -> None:
        self._step_count += 1
        if isinstance(decision, Stop):
            self._status = EpisodeStatus.STOPPED

    async def apply(
        self,
        decision: Decision,
        *,
        maximum_wait_seconds: float = 5.0,
        request_id: str | None = None,
    ) -> DecisionResult:
        """Apply an external decision through the same bounded episode and host."""

        if self._session is None:
            raise EpisodeError("episode has not been reset")
        session = self._session
        result = await self._within_deadline(
            lambda: dispatch_decision(
                session,
                decision,
                maximum_wait_seconds=maximum_wait_seconds,
                request_id=request_id,
            )
        )
        self._finish_step(decision)
        return result

    async def step(self, runner: AgentRunner) -> AgentStep:
        """Run one isolated decision within the episode's total deadline."""

        if self._session is None:
            raise EpisodeError("episode has not been reset")
        session = self._session
        step = await self._within_deadline(lambda: runner.step(session))
        self._finish_step(step.reply.decision)
        return step

    async def run_until_stop(
        self,
        runner: AgentRunner,
        *,
        maximum_decisions: int = 32,
    ) -> tuple[AgentStep, ...]:
        """Run a policy to an explicit stop without giving it reset authority."""

        if maximum_decisions < 1:
            raise ValueError("maximum decisions must be positive")
        steps: list[AgentStep] = []
        for _ in range(maximum_decisions):
            step = await self.step(runner)
            steps.append(step)
            if isinstance(step.reply.decision, Stop):
                return tuple(steps)
        self._status = EpisodeStatus.READY
        raise EpisodeError("policy exceeded the maximum decision count")

    async def close(self) -> None:
        if self._closed:
            return
        if self._step_in_flight:
            raise EpisodeError("cannot close while an agent step is outstanding")
        self._closed = True
        if self._session is not None:
            await self._session.close()
        self._status = EpisodeStatus.CLOSED
