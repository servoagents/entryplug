"""OpenEnv application factory kept outside the Entryplug core package."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from openenv.core.env_server import create_app  # type: ignore[import-untyped]

from entryplug_openenv.environment import EntryplugEnvironment
from entryplug_openenv.models import EntryplugAction, EntryplugObservation, EntryplugState


def create_entryplug_app(
    environment_factory: Callable[[], EntryplugEnvironment],
) -> Any:
    """Create the upstream OpenEnv server without forking its transport."""

    return create_app(
        environment_factory,
        EntryplugAction,
        EntryplugObservation,
        env_name="entryplug",
        max_concurrent_envs=1,
        state_cls=EntryplugState,
    )
