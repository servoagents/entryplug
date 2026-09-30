"""Interactive, explicitly simulated bodies and service-owned finite playback."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Mapping
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
from entryplug_app.contracts import AppError, digest, text, utc_now
from entryplug_app.demo import DemoCamera
from entryplug_app.drivers import TurnContext
from entryplug_app.ports import EmbodimentPort

CAMERA_SCENARIOS = {
    "entries": [
        (0, "idle"),
        (2, "enter_a"),
        (4, "presence_a"),
        (6, "enter_b"),
        (8, "exit_a"),
        (10, "enter_a"),
        (12, "presence_a"),
    ],
    "outage": [
        (0, "idle"),
        (2, "enter_a"),
        (4, "lost"),
        (8, "recover"),
        (10, "enter_b"),
        (12, "presence_a"),
    ],
    "stale": [(0, "idle"), (2, "enter_a"), (5, "stale"), (9, "recover"), (11, "enter_b")],
}


class SimulatedDevice:
    def __init__(self, identifier: str, name: str, kind: str):
        self.kind = kind
        self.epoch = uuid.uuid4().hex
        self.sequence = 0
        self.state: dict[str, Any] = {
            "waypoint": "dock",
            "battery": 96,
            "obstacle": False,
            "occupied": False,
            "light": False,
            "temperature": 21,
        }
        read = "robot.observe" if kind == "rover" else "room.observe"
        write = "robot.move" if kind == "rover" else "room.set_light"
        parameters = (
            {"waypoint": {"type": "string", "enum": ["dock", "aisle", "station"]}}
            if kind == "rover"
            else {"on": {"type": "boolean"}}
        )
        host = OperationHost(
            [
                CapabilitySpec(
                    read,
                    "1",
                    "Read the simulated body's state",
                    False,
                    3,
                    0.2,
                    self.validate_read,
                    self.observe,
                    {"type": "object", "properties": {}, "additionalProperties": False},
                ),
                CapabilitySpec(
                    write,
                    "1",
                    "Change only this simulated body",
                    False,
                    3,
                    0.2,
                    self.validate_write,
                    self.act,
                    {
                        "type": "object",
                        "properties": parameters,
                        "required": list(parameters),
                        "additionalProperties": False,
                    },
                ),
            ]
        )
        self.port = EmbodimentPort(
            identifier,
            name,
            Session(host, owns_runtime=True),
            simulated=True,
            body_type=kind,
            protocol="simulation",
        )

    @staticmethod
    def validate_read(arguments: Any) -> dict[str, Any]:
        if arguments != {}:
            raise ValueError("Observation takes no arguments")
        return {}

    def validate_write(self, arguments: Any) -> dict[str, Any]:
        key = "waypoint" if self.kind == "rover" else "on"
        if not isinstance(arguments, Mapping) or set(arguments) != {key}:
            raise ValueError("Supply exactly the declared action argument")
        if self.kind == "rover":
            if arguments[key] not in ("dock", "aisle", "station"):
                raise ValueError("Unknown waypoint")
        elif type(arguments[key]) is not bool:
            raise ValueError("on must be a boolean")
        return dict(arguments)

    def observation(self) -> dict[str, Any]:
        return {
            "source": self.port.body_id,
            "simulated": True,
            "at": utc_now(),
            "age_ms": 0,
            **self.state,
        }

    async def observe(self, context: OperationContext, arguments: Any) -> OperationResult:
        return OperationResult(Lifecycle.SUCCEEDED, MotionState.IDLE, self.observation())

    async def act(self, context: OperationContext, arguments: Any) -> OperationResult:
        await asyncio.sleep(0.15)
        if not self.port.available:
            return OperationResult(Lifecycle.FAILED, MotionState.IDLE, reason_code="SOURCE_LOST")
        if self.kind == "rover":
            if self.state["obstacle"]:
                return OperationResult(
                    Lifecycle.FAILED,
                    MotionState.IDLE,
                    self.observation(),
                    reason_code="SIMULATED_OBSTACLE",
                )
            self.state.update(
                waypoint=arguments["waypoint"], battery=max(0, self.state["battery"] - 3)
            )
        else:
            self.state["light"] = arguments["on"]
        self.port.last_seen = utc_now()
        return OperationResult(Lifecycle.SUCCEEDED, MotionState.IDLE, self.observation())

    def step(self, action: str) -> dict[str, Any]:
        allowed = {"lost", "recover", "reset", "tick"}
        allowed |= {"obstacle", "clear"} if self.kind == "rover" else {"arrive", "leave"}
        if action not in allowed:
            raise AppError("validation_error", "Unknown simulation action")
        self.sequence += 1
        event_type = "simulation.changed"
        if action == "lost":
            self.port.available = False
            event_type = "source.lost"
        elif action in {"recover", "reset"}:
            self.port.available = True
            self.epoch = uuid.uuid4().hex
            event_type = "source.revalidated"
            if action == "reset":
                self.state.update(
                    waypoint="dock",
                    battery=96,
                    obstacle=False,
                    occupied=False,
                    light=False,
                    temperature=21,
                )
        elif not self.port.available:
            raise AppError("source_lost", "Reconnect the simulated body first", 409)
        elif action in {"obstacle", "clear"}:
            self.state["obstacle"] = action == "obstacle"
        elif action in {"arrive", "leave"}:
            self.state["occupied"] = action == "arrive"
        self.port.last_seen = utc_now()
        return {
            **self.observation(),
            "type": event_type,
            "epoch": self.epoch,
            "cursor": self.sequence,
            "occurrence": digest([self.port.body_id, self.epoch, self.sequence]),
        }


class SimulationDriver:
    """Visible deterministic observe/action/report policy, with no model or network."""

    async def turn(self, context: TurnContext) -> str:
        async def say(message: str) -> None:
            if context.on_text:
                await context.on_text(message + "\n")
            await asyncio.sleep(0.25)

        allowed = context.definition.access.allow
        await say("Simulated agent: checking the selected body's observation.")
        capability = next(
            (n for n in ("camera.snapshot", "robot.observe", "room.observe") if n in allowed), None
        )
        if capability is None:
            raise AppError("capability_unavailable", "The simulated agent needs an observation")
        operation = await context.tools.call(capability, {})
        if operation["lifecycle"] != "succeeded":
            return "Simulated agent: observation failed; no action taken."
        state = operation.get("result") or {}
        if capability == "camera.snapshot":
            if context.observation.get("type") != "person.entered":
                return "Simulated agent: no labelled entry event; observation recorded."
            message = "Simulated agent: labelled entry confirmed; camera evidence attached."
        elif capability == "robot.observe":
            if state.get("obstacle"):
                message = "Simulated rover: obstacle detected; staying at the current waypoint."
            else:
                waypoint = {"dock": "aisle", "aisle": "station", "station": "dock"}.get(
                    str(state.get("waypoint", "dock")), "dock"
                )
                await say(f"Simulated agent: moving to {waypoint} using robot.move.")
                outcome = await context.tools.call("robot.move", {"waypoint": waypoint})
                message = (
                    f"Simulated rover reached {waypoint}."
                    if outcome["lifecycle"] == "succeeded"
                    else "Simulated rover move failed; inspect its operation."
                )
        else:
            on = bool(state.get("occupied"))
            await say(
                f"Simulated agent: room is {'occupied' if on else 'empty'}; updating the light."
            )
            outcome = await context.tools.call("room.set_light", {"on": on})
            message = (
                f"Simulated room: light {'on' if on else 'off'}."
                if outcome["lifecycle"] == "succeeded"
                else "Simulated light update failed."
            )
        await say(message)
        if "alerts.emit" in allowed:
            await context.tools.call("alerts.emit", {"message": message})
        return message + " No LLM was used."


class Simulations:
    def __init__(self, service: Any, camera: DemoCamera | None):
        self.service = service
        self.enabled = camera is not None
        self.bodies: dict[str, Any] = {}
        self.players: dict[str, dict[str, Any]] = {}
        self.lock = asyncio.Lock()
        if camera:
            self.register(camera, "camera")
            self.register(SimulatedDevice("demo.rover", "Warehouse rover", "rover"), "rover")
            self.register(SimulatedDevice("demo.room", "Smart room", "room"), "room")

    def register(self, body: Any, kind: str) -> None:
        identifier = body.port.body_id
        self.bodies[identifier] = body
        self.service.ports[identifier] = body.port
        self.players[identifier] = {
            "kind": kind,
            "scenario": "entries" if kind == "camera" else "tour",
            "position_s": 0.0,
            "playing": False,
            "index": 0,
            "started": 0.0,
            "generation": 0,
        }

    async def restore(self) -> None:
        if not self.enabled:
            return
        for record in await self.service.store.list("simulation_bodies"):
            self.register(self.make(record["id"], record["name"], record["kind"]), record["kind"])

    @staticmethod
    def make(identifier: str, name: str, kind: str) -> Any:
        return (
            DemoCamera(identifier, name)
            if kind == "camera"
            else SimulatedDevice(identifier, name, kind)
        )

    async def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.enabled:
            raise AppError("demo_disabled", "Simulations are disabled for this service", 409)
        kind = payload.get("kind")
        if kind not in {"camera", "rover", "room"} or set(payload) - {"kind", "name"}:
            raise AppError("validation_error", "Choose camera, rover or room and a name")
        name = text(payload.get("name"), "body name", 160)
        async with self.lock:
            if len(self.bodies) >= 24:
                raise AppError("simulation_limit", "At most 24 simulated bodies per workspace", 409)
            record = {"id": "demo." + uuid.uuid4().hex[:12], "name": name, "kind": kind}
            await self.service.store.commit(
                [("simulation_bodies", record["id"], record)], "simulation.created", record
            )
            self.register(self.make(record["id"], name, kind), kind)
            return self.describe(record["id"])

    def describe(self, identifier: str) -> dict[str, Any]:
        body, player = self.bodies[identifier], self.players[identifier]
        position = (
            time.monotonic() - player["started"] if player["playing"] else player["position_s"]
        )
        return {
            "id": identifier,
            "name": body.port.name,
            "kind": player["kind"],
            "simulated": True,
            "available": body.port.available,
            "playing": player["playing"],
            "position_s": min(14.24, position),
            "duration_s": 14.24,
            "scenario": player["scenario"],
            "sampled_at": utc_now(),
            "state": body.observation(),
            "scenarios": list(CAMERA_SCENARIOS) if player["kind"] == "camera" else ["tour"],
            "recording": "/assets/city-walk.webm" if player["kind"] == "camera" else None,
        }

    def snapshot(self) -> list[dict[str, Any]]:
        return [self.describe(identifier) for identifier in self.bodies]

    def timeline(self, identifier: str) -> list[tuple[int, str]]:
        p = self.players[identifier]
        if p["kind"] == "camera":
            return CAMERA_SCENARIOS[p["scenario"]]
        if p["kind"] == "rover":
            return [(0, "tick"), (3, "tick"), (6, "obstacle"), (9, "clear"), (12, "tick")]
        return [(0, "leave"), (3, "arrive"), (8, "leave"), (11, "arrive")]

    async def control(self, identifier: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with self.lock:
            if identifier not in self.bodies:
                raise AppError("not_found", "Simulated body not found", 404)
            body, p = self.bodies[identifier], self.players[identifier]
            action = payload.get("action")
            if action not in {"play", "pause", "reset", "step", "scenario", "inject"}:
                raise AppError("validation_error", "Unknown playback command")
            if (
                action == "scenario"
                and payload.get("scenario") not in self.describe(identifier)["scenarios"]
            ):
                raise AppError("validation_error", "Unknown scenario")
            if p["playing"]:
                p["position_s"] = min(14.24, time.monotonic() - p["started"])
            p["playing"] = False
            p["generation"] += 1
            if action in {"reset", "scenario"} or (action == "play" and p["position_s"] >= 14.24):
                p.update(position_s=0.0, index=0)
                await self.service.ingest(
                    body.step("recover" if p["kind"] == "camera" else "reset")
                )
            if action == "scenario":
                p["scenario"] = payload["scenario"]
            if action == "play":
                p.update(playing=True, started=time.monotonic() - p["position_s"])
                self.service.spawn(self.play(identifier, p["generation"]))
            elif action == "step":
                timeline = self.timeline(identifier)
                if p["index"] < len(timeline):
                    at, cue = timeline[p["index"]]
                    p.update(index=p["index"] + 1, position_s=float(at))
                    await self.service.ingest(body.step(cue))
            elif action == "inject":
                await self.service.ingest(body.step(payload.get("event", "")))
            result = self.describe(identifier)
            await self.service.store.commit([], "simulation.controlled", result)
            return result

    async def play(self, identifier: str, generation: int) -> None:
        while not self.service.closed:
            async with self.lock:
                p = self.players[identifier]
                if generation != p["generation"] or not p["playing"]:
                    return
                position = time.monotonic() - p["started"]
                timeline = self.timeline(identifier)
                try:
                    while p["index"] < len(timeline) and timeline[p["index"]][0] <= position:
                        cue = timeline[p["index"]][1]
                        p["index"] += 1
                        await self.service.ingest(self.bodies[identifier].step(cue))
                    if position >= 14.24:
                        p.update(playing=False, position_s=14.24)
                        await self.service.store.commit(
                            [], "simulation.finished", self.describe(identifier)
                        )
                        return
                except AppError as error:
                    p.update(playing=False, position_s=min(14.24, position))
                    await self.service.store.commit(
                        [], "simulation.blocked", {"id": identifier, "code": error.code}
                    )
                    return
            await asyncio.sleep(0.1)
