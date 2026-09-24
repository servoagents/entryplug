"""Typed OpenEnv client for the Entryplug environment."""

from __future__ import annotations

from typing import Any, cast

from openenv.core.client_types import StepResult  # type: ignore[import-untyped]
from openenv.core.env_client import EnvClient  # type: ignore[import-untyped]

from entryplug_openenv.models import EntryplugAction, EntryplugObservation, EntryplugState


class EntryplugEnvClient(
    EnvClient[EntryplugAction, EntryplugObservation, EntryplugState]  # type: ignore[misc]
):
    def _step_payload(self, action: EntryplugAction) -> dict[str, Any]:
        return cast(dict[str, Any], action.model_dump(mode="json"))

    def _parse_result(self, payload: dict[str, Any]) -> StepResult[EntryplugObservation]:
        observation_data = dict(payload.get("observation") or {})
        observation_data["reward"] = payload.get("reward")
        observation_data["done"] = bool(payload.get("done", False))
        observation = cast(
            EntryplugObservation,
            EntryplugObservation.model_validate(observation_data),
        )
        return StepResult(
            observation=observation,
            reward=payload.get("reward"),
            done=observation.done,
            metadata=payload.get("metadata"),
        )

    def _parse_state(self, payload: dict[str, Any]) -> EntryplugState:
        return cast(EntryplugState, EntryplugState.model_validate(payload))
