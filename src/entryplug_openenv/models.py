"""OpenEnv 0.5 models for one Entryplug decision per environment step."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from openenv.core.env_server.types import Action, Observation, State  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field


class ActDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["act"] = "act"
    capability: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    request_id: str = Field(min_length=1, max_length=255)


class WaitDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["wait"] = "wait"
    operation_id: str = Field(min_length=1)
    timeout_seconds: float = Field(gt=0)


class CancelDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["cancel"] = "cancel"
    operation_id: str = Field(min_length=1)


class InspectDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["inspect"] = "inspect"
    reference: str = Field(min_length=1)
    detail: Literal["summary", "result"] = "summary"


class StopDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["stop"] = "stop"
    reason: str = Field(default="policy complete", min_length=1)


DecisionModel = Annotated[
    ActDecision | WaitDecision | CancelDecision | InspectDecision | StopDecision,
    Field(discriminator="kind"),
]


class EntryplugAction(Action):  # type: ignore[misc]
    """One typed agent decision; act requests require a stable mutation ID."""

    decision: DecisionModel


class EntryplugState(State):  # type: ignore[misc]
    """Public episode state without evaluator truth or private reward."""

    generation: int = Field(default=0, ge=0)
    seed: int | None = Field(default=None, ge=0)
    runtime_id: str | None = None
    status: Literal["ready", "running", "stopped", "timed_out", "closed"] = "closed"
    public_metadata: dict[str, Any] = Field(default_factory=dict)
    decision_count: int = Field(default=0, ge=0)
    wait_count: int = Field(default=0, ge=0)
    elapsed_wall_time_seconds: float = Field(default=0.0, ge=0)
    elapsed_sim_time_seconds: float | None = Field(default=None, ge=0)


class EntryplugObservation(Observation):  # type: ignore[misc]
    """Bounded public result of reset or exactly one agent decision."""

    state: EntryplugState
    decision_kind: Literal["act", "wait", "cancel", "inspect", "stop"] | None = None
    result: dict[str, Any] | None = None
