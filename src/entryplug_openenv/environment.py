"""Optional OpenEnv adapter over Entryplug's existing episode and operation host."""

from __future__ import annotations

import asyncio
import math
import threading
import time
from collections.abc import Callable, Coroutine, Mapping
from concurrent.futures import Future
from typing import Any, TypeVar, cast

from openenv.core.env_server.interfaces import Environment  # type: ignore[import-untyped]

from entryplug.agent import Act, Cancel, Decision, Inspect, Stop, Wait
from entryplug.episode import EpisodeController, EpisodeFactory, EpisodeState
from entryplug.operation import OperationSnapshot
from entryplug_openenv.models import (
    ActDecision,
    CancelDecision,
    EntryplugAction,
    EntryplugObservation,
    EntryplugState,
    InspectDecision,
    StopDecision,
    WaitDecision,
)

ResultT = TypeVar("ResultT")


class _AsyncBridge:
    """Keep an episode's asyncio resources on one owned background loop."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._closed = False
        self._thread = threading.Thread(
            target=self._run,
            name="entryplug-openenv-episode",
            daemon=True,
        )
        self._thread.start()
        self._ready.wait()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()
        pending = asyncio.all_tasks(self._loop)
        for task in pending:
            task.cancel()
        if pending:
            self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self._loop.close()

    def _submit(self, work: Callable[[], Coroutine[Any, Any, ResultT]]) -> Future[ResultT]:
        if self._closed:
            raise RuntimeError("OpenEnv episode bridge is closed")
        return asyncio.run_coroutine_threadsafe(work(), self._loop)

    async def call_async(self, work: Callable[[], Coroutine[Any, Any, ResultT]]) -> ResultT:
        future = self._submit(work)
        try:
            return await asyncio.wrap_future(future)
        except asyncio.CancelledError:
            future.cancel()
            raise

    def call(self, work: Callable[[], Coroutine[Any, Any, ResultT]]) -> ResultT:
        return self._submit(work).result()

    def close(self, work: Callable[[], Coroutine[Any, Any, None]]) -> None:
        if self._closed:
            return
        try:
            self.call(work)
        finally:
            self._closed = True
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5.0)
            if self._thread.is_alive():
                raise RuntimeError("OpenEnv episode loop did not stop")


def _decision(action: EntryplugAction) -> tuple[Decision, str | None]:
    value = action.decision
    if isinstance(value, ActDecision):
        return Act(value.capability, value.arguments), value.request_id
    if isinstance(value, WaitDecision):
        return Wait(value.operation_id, value.timeout_seconds), None
    if isinstance(value, CancelDecision):
        return Cancel(value.operation_id), None
    if isinstance(value, InspectDecision):
        return Inspect(value.reference, value.detail), None
    if isinstance(value, StopDecision):
        return Stop(value.reason), None
    raise TypeError("unsupported OpenEnv decision")


def _plain(value: object) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _result(value: object) -> dict[str, Any] | None:
    if isinstance(value, OperationSnapshot):
        return {
            "operation_id": value.operation_id,
            "runtime_id": value.runtime_id,
            "request_id": value.request_id,
            "capability": value.capability,
            "lifecycle": value.lifecycle.value,
            "motion_state": value.motion_state.value,
            "phase": value.phase,
            "cancel_requested": value.cancel_requested,
            "reason_code": value.reason_code,
            "result": _plain(value.result),
        }
    if isinstance(value, Mapping):
        return cast(dict[str, Any], _plain(value))
    return None


async def _observe(controller: EpisodeController) -> EpisodeState:
    return controller.observe()


class EntryplugEnvironment(
    Environment[EntryplugAction, EntryplugObservation, EntryplugState]  # type: ignore[misc]
):
    """Expose reset, one decision per step, and public state through OpenEnv."""

    SUPPORTS_CONCURRENT_SESSIONS = False

    def __init__(
        self,
        episode_factory: EpisodeFactory,
        *,
        episode_timeout_seconds: float = 60.0,
        observation_window_seconds: float = 5.0,
    ) -> None:
        super().__init__()
        if not math.isfinite(observation_window_seconds) or observation_window_seconds <= 0:
            raise ValueError("observation window must be finite and positive")
        self._controller = EpisodeController(
            episode_factory,
            timeout_seconds=episode_timeout_seconds,
        )
        self._observation_window_seconds = observation_window_seconds
        self._bridge = _AsyncBridge()
        self._decision_count = 0
        self._wait_count = 0
        self._started = 0.0
        self._done = False
        self._closed = False
        self._last_state = EntryplugState()

    def _maximum_wait(self, timeout_s: float | None) -> float:
        if timeout_s is None:
            return self._observation_window_seconds
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("step timeout must be finite and positive")
        return min(timeout_s, self._observation_window_seconds)

    def _after_reset(self, state: EpisodeState) -> EntryplugObservation:
        self._decision_count = 0
        self._wait_count = 0
        self._started = time.monotonic()
        self._done = False
        public = self._state_from_episode(state)
        return EntryplugObservation(
            state=public,
            done=False,
            reward=None,
            metadata={"timing_mode": "wall_paced", "private_reward_exposed": False},
        )

    def _state_from_episode(self, state: EpisodeState) -> EntryplugState:
        elapsed = max(0.0, time.monotonic() - self._started) if self._started else 0.0
        public = EntryplugState(
            episode_id=state.episode_id,
            step_count=state.step_count,
            generation=state.generation,
            seed=state.seed,
            runtime_id=state.runtime_id,
            status=state.status.value,
            public_metadata=dict(state.public_metadata),
            decision_count=self._decision_count,
            wait_count=self._wait_count,
            elapsed_wall_time_seconds=round(elapsed, 6),
            elapsed_sim_time_seconds=None,
        )
        self._last_state = public
        return public

    def reset(
        self,
        seed: int | None = None,
        episode_id: str | None = None,
        **_: Any,
    ) -> EntryplugObservation:
        if episode_id is not None:
            raise ValueError("Entryplug assigns episode IDs at successful reset")
        state = self._bridge.call(lambda: self._controller.reset(seed or 0))
        return self._after_reset(state)

    async def reset_async(
        self,
        seed: int | None = None,
        episode_id: str | None = None,
        **_: Any,
    ) -> EntryplugObservation:
        if episode_id is not None:
            raise ValueError("Entryplug assigns episode IDs at successful reset")
        state = await self._bridge.call_async(lambda: self._controller.reset(seed or 0))
        return self._after_reset(state)

    def _after_step(
        self,
        action: EntryplugAction,
        decision: Decision,
        result: object,
    ) -> EntryplugObservation:
        self._decision_count += 1
        if isinstance(decision, Wait):
            self._wait_count += 1
        self._done = isinstance(decision, Stop)
        state = self._bridge.call(lambda: _observe(self._controller))
        return EntryplugObservation(
            state=self._state_from_episode(state),
            decision_kind=action.decision.kind,
            result=_result(result),
            done=self._done,
            reward=None,
            metadata={"timing_mode": "wall_paced", "private_reward_exposed": False},
        )

    def step(
        self,
        action: EntryplugAction,
        timeout_s: float | None = None,
        **_: Any,
    ) -> EntryplugObservation:
        if self._done:
            raise RuntimeError("episode is done; reset before another step")
        decision, request_id = _decision(action)
        maximum_wait = self._maximum_wait(timeout_s)
        result = self._bridge.call(
            lambda: self._controller.apply(
                decision,
                maximum_wait_seconds=maximum_wait,
                request_id=request_id,
            )
        )
        return self._after_step(action, decision, result)

    async def step_async(
        self,
        action: EntryplugAction,
        timeout_s: float | None = None,
        **_: Any,
    ) -> EntryplugObservation:
        if self._done:
            raise RuntimeError("episode is done; reset before another step")
        decision, request_id = _decision(action)
        maximum_wait = self._maximum_wait(timeout_s)
        result = await self._bridge.call_async(
            lambda: self._controller.apply(
                decision,
                maximum_wait_seconds=maximum_wait,
                request_id=request_id,
            )
        )
        return self._after_step(action, decision, result)

    @property
    def state(self) -> EntryplugState:
        if self._closed or self._last_state.episode_id is None:
            return self._last_state
        state = self._bridge.call(lambda: _observe(self._controller))
        return self._state_from_episode(state)

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._bridge.close(lambda: self._controller.close())
        finally:
            self._closed = True
            self._last_state = self._last_state.model_copy(update={"status": "closed"})
