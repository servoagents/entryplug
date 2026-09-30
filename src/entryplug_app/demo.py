"""Explicitly SIMULATED labelled source; no computer-vision claim."""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from entryplug.core.operation import (
    CapabilitySpec,
    Lifecycle,
    MotionState,
    OperationContext,
    OperationHost,
    OperationResult,
)
from entryplug.harness.session import Session
from entryplug_app.contracts import AppError, digest, utc_now
from entryplug_app.ports import EmbodimentPort

# Public labelled fixture, separate from the evaluator's expected results.
SEQUENCE = (
    "idle",
    "enter_a",
    "presence_a",
    "enter_b",
    "exit_a",
    "enter_a",
    "lost",
    "recover",
    "restart",
    "enter_a",
    "stale",
    "recover",
)


class DemoCamera:
    def __init__(
        self, identifier: str = "demo.access-camera", name: str = "Access camera · SIMULATED"
    ) -> None:
        self.epoch = uuid.uuid4().hex
        self.present: set[str] = set()
        self.entries: dict[str, int] = {}
        self.sequence = 0
        self.frame_at = time.monotonic()
        self.frame_timestamp = utc_now()
        self.stale = False
        self.last_observation: dict[str, Any] | None = None
        specs = []
        for capability_name in ("camera.snapshot", "person.events"):
            specs.append(
                CapabilitySpec(
                    name=capability_name,
                    version="1",
                    description="SIMULATED labelled camera fixture",
                    motion_producing=False,
                    deadline_seconds=2,
                    cancel_grace_seconds=0.2,
                    validate=self._validate,
                    run=self._snapshot,
                    input_schema={
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                )
            )
        self.host = OperationHost(specs)
        self.port = EmbodimentPort(
            identifier,
            name,
            Session(self.host, owns_runtime=True),
            simulated=True,
            body_type="camera",
            protocol="simulation",
        )

    @staticmethod
    def _validate(arguments: Any) -> dict[str, Any]:
        if arguments:
            raise ValueError("snapshot takes no arguments")
        return {}

    async def _snapshot(self, context: OperationContext, arguments: Any) -> OperationResult:
        if not self.port.available:
            return OperationResult(Lifecycle.FAILED, MotionState.IDLE, reason_code="SOURCE_LOST")
        # A snapshot captures the current simulated scene; the stale fixture deliberately
        # models a producer returning an old frame even when a new one is requested.
        if not self.stale:
            self.frame_at = time.monotonic()
            self.frame_timestamp = utc_now()
            self.port.last_seen = self.frame_timestamp
        return OperationResult(Lifecycle.SUCCEEDED, MotionState.IDLE, self.observation())

    def observation(self) -> dict[str, Any]:
        return {
            "source": self.port.body_id,
            "epoch": self.epoch,
            "sequence": self.sequence,
            "at": self.frame_timestamp,
            "age_ms": max(0, (time.monotonic() - self.frame_at) * 1000),
            "present": sorted(self.present),
            "simulated": True,
            "available": self.port.available,
            "label": "SIMULATED — labelled sequence, not a real detector",
            "recording": {
                "url": "/assets/city-walk.webm",
                "role": "illustrative_recording",
                "note": "Events are scripted, not detected from these pixels",
            },
        }

    def step(self, action: str) -> dict[str, Any]:
        if action not in set(SEQUENCE):
            raise AppError("validation_error", "Unknown demo step")
        self.sequence += 1
        self.frame_at = time.monotonic()
        self.port.last_seen = utc_now()
        self.frame_timestamp = self.port.last_seen
        self.stale = action == "stale"
        event_type, person, index = "person.present", None, None
        if action == "lost":
            self.port.available = False
            event_type = "source.lost"
        elif action in {"recover", "restart"}:
            self.port.available = True
            self.epoch = uuid.uuid4().hex
            self.present.clear()
            self.entries.clear()
            event_type = "source.revalidated"
        elif not self.port.available:
            raise AppError(
                "source_lost", "Recover the simulated source before sending observations", 409
            )
        elif action.startswith("enter_"):
            person = action[-1]
            if person not in self.present:
                self.present.add(person)
                self.entries[person] = self.entries.get(person, 0) + 1
                event_type, index = "person.entered", self.entries[person]
        elif action.startswith("exit_"):
            person = action[-1]
            self.present.discard(person)
            event_type = "person.exited"
        elif action == "stale":
            self.frame_at -= 120
            self.frame_timestamp = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
            event_type = "person.entered"
            person, index = "stale", 1
        occurrence = [self.port.body_id, self.epoch, "entrance", person, index]
        event = {
            **self.observation(),
            "type": event_type,
            "zone": "entrance",
            "object_id": person,
            "entry_index": index,
            "occurrence": digest(occurrence),
            "cursor": self.sequence,
        }
        self.last_observation = event
        return event
