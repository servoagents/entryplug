"""One application owner for missions, turns, durable intent and recovery."""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import time
import uuid
from collections.abc import Coroutine
from typing import Any

from entryplug.core.operation import AdmissionError
from entryplug_app.contracts import (
    API_VERSION,
    BUILD_ID,
    RUN_TERMINAL,
    AppError,
    canonical,
    digest,
    text,
    utc_now,
    validate_definition,
)
from entryplug_app.demo import DemoCamera
from entryplug_app.drivers import AgentDriver, RulesDriver, ScriptedDriver, TurnContext
from entryplug_app.media import public_content
from entryplug_app.ports import EmbodimentPort
from entryplug_app.store import Store
from entryplug_app.workspace import Workspace, WorkspaceLock

OP_TERMINAL = {"succeeded", "failed", "canceled", "rejected", "indeterminate"}


def new_id(prefix: str) -> str:
    return prefix + "_" + uuid.uuid4().hex


class ApplicationService:
    def __init__(
        self,
        workspace: Workspace,
        *,
        ports: list[EmbodimentPort] | None = None,
        demo: bool = True,
        allow_scripted: bool = False,
        event_retention: int = 10_000,
    ):
        self.workspace = workspace
        self.owner = WorkspaceLock(workspace)
        self.store = Store(workspace.state / "missions.sqlite3", event_retention=event_retention)
        self.lock = asyncio.Lock()
        self.demo = DemoCamera() if demo else None
        self.ports = {p.body_id: p for p in ports or []}
        if self.demo:
            self.ports[self.demo.port.body_id] = self.demo.port
        self.drivers: dict[str, AgentDriver] = {"rules": RulesDriver()}
        if allow_scripted:
            self.drivers["scripted"] = ScriptedDriver()
        self.tasks: set[asyncio.Task[Any]] = set()
        self.runners: dict[str, asyncio.Task[Any]] = {}
        self.wake: dict[str, asyncio.Event] = {}
        self.turn_tasks: dict[str, asyncio.Task[Any]] = {}
        self.closed = False
        self.started = False
        self.subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    def transient(self, event: dict[str, Any]) -> None:
        for queue in self.subscribers:
            if queue.full():
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait({"type": "resync", "code": "slow_consumer"})
            else:
                queue.put_nowait(event)

    def spawn(self, work: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        task = asyncio.create_task(work)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def open(self) -> None:
        self.owner.acquire()
        try:
            await self.store.open()
            self.started = True
            async with self.lock:
                for op in await self.store.list("operations"):
                    if op["lifecycle"] not in OP_TERMINAL:
                        sent = op["dispatch"] not in {"pending", "waiting_approval"}
                        op.update(
                            lifecycle="indeterminate" if sent else "failed",
                            reason_code="RESTART_UNCONFIRMED" if sent else "NOT_DISPATCHED",
                            effect_state="unknown" if sent and op["physical_effects"] else "none",
                        )
                        await self._record(
                            "operations", op, "operation.recovered", op.get("run_id")
                        )
                for approval in await self.store.list("approvals"):
                    if approval["status"] == "pending":
                        approval.update(status="invalidated", reason_code="service_restarted")
                        await self._record(
                            "approvals", approval, "approval.invalidated", approval["run_id"]
                        )
                for turn in await self.store.list("turns"):
                    if turn["status"] == "running":
                        turn.update(status="interrupted", completed_at=utc_now())
                        await self._record("turns", turn, "turn.interrupted", turn["run_id"])
                for run in await self.store.list("runs"):
                    if run["lifecycle"] not in RUN_TERMINAL:
                        run.update(
                            activity="recovering",
                            health="blocked",
                            reason_code="revalidation_required",
                            pending=[],
                            active_turn_id=None,
                        )
                        await self._record("runs", run, "mission.coverage_gap", run["id"])
                await self.store.commit([], "service.started", {"build_id": BUILD_ID})
        except BaseException:
            await self.store.close()
            self.owner.release()
            raise

    async def _record(
        self, kind: str, document: dict[str, Any], event: str, run_id: str | None = None
    ) -> None:
        document["updated_at"] = utc_now()
        await self.store.commit([(kind, document["id"], document)], event, document, run_id=run_id)

    async def bodies(self) -> list[dict[str, Any]]:
        return [await port.describe() for port in self.ports.values()]

    async def topology(self) -> dict[str, Any]:
        nodes, edges = [], []
        for body in await self.bodies():
            meta = {
                "origin": body["id"],
                "first_seen": body["first_seen"],
                "last_seen": body["last_seen"],
                "expires_after_s": 30,
                "status": body["status"],
                "provenance": body["provenance"],
                "simulated": body["simulated"],
                "evidence_refs": [body["runtime_id"]],
            }
            nodes.append({"id": body["id"], "kind": "device", "label": body["name"], **meta})
            for cap in body["capabilities"]:
                identifier = body["id"] + ":" + cap["name"]
                nodes.append({"id": identifier, "kind": "capability", "label": cap["name"], **meta})
                edges.append(
                    {
                        "id": identifier + ":exposes",
                        "source": body["id"],
                        "target": identifier,
                        "kind": "exposes",
                        **meta,
                    }
                )
        return {"nodes": nodes, "edges": edges, "at": utc_now()}

    async def snapshot(self) -> dict[str, Any]:
        async with self.lock:
            state = await self.store.snapshot()
            state["attachments"] = [
                {k: v for k, v in a.items() if k != "token_hash"} for a in state["attachments"]
            ]
            return {
                **state,
                "api_version": API_VERSION,
                "build_id": BUILD_ID,
                "bodies": await self.bodies(),
                "topology": await self.topology(),
            }

    def _driver(self, definition: Any) -> AgentDriver:
        key = (
            definition.agent.profile
            if definition.agent.driver == "native"
            else definition.agent.driver
        )
        if key not in self.drivers:
            raise AppError(
                "driver_unavailable", "Configure this driver/profile before starting", 409
            )
        return self.drivers[key]

    async def validate(self, value: Any) -> dict[str, Any]:
        definition = validate_definition(value)
        port = self.ports.get(definition.body.selector)
        warnings = []
        if not port:
            warnings.append("body_not_connected")
        else:
            names = {c["name"] for c in (await port.describe())["capabilities"]} | {"alerts.emit"}
            if not set(definition.access.allow) <= names:
                raise AppError(
                    "validation_error", "Access refers to capabilities unavailable on this body"
                )
        return {
            "valid": True,
            "definition": definition.to_dict(),
            "instructions_hash": digest(definition.instructions),
            "warnings": warnings,
        }

    async def _require_body(self, definition: Any) -> EmbodimentPort:
        port = self.ports.get(definition.body.selector)
        if not port or not port.available:
            raise AppError("source_lost", "The selected body is not connected and validated", 409)
        names = {c["name"] for c in (await port.describe())["capabilities"]}
        if not set(definition.body.required_capabilities) <= names:
            raise AppError(
                "capability_unavailable", "Required body capabilities are unavailable", 409
            )
        return port

    async def command(
        self,
        action: str,
        payload: dict[str, Any],
        key: str,
        *,
        actor: str = "owner",
        revision: int | None = None,
    ) -> dict[str, Any]:
        """Durably deduplicate effectful interface commands under one owner."""
        text(key, "Idempotency-Key", 200)
        payload = dict(payload)
        if action == "operation.create":
            payload.setdefault("arguments", {})
        fingerprint = digest({"action": action, "payload": payload, "revision": revision})
        follow: list[tuple[str, str]] = []
        async with self.lock:
            if actor != "owner" and action not in {"operation.create", "operation.cancel"}:
                raise AppError(
                    "forbidden", "External attachments may only operate their granted body", 403
                )
            if self.closed:
                raise AppError("service_stopping", "Service is closing admissions", 503)
            previous = await self.store.request(actor, key, fingerprint)
            if previous is not None:
                return previous
            if not action.endswith(("stop", "pause", "cancel", "ack", "revoke")):
                await self.store.check_capacity()
            port: EmbodimentPort | None
            records: list[tuple[str, str, dict[str, Any]]] = []
            run_id = payload.get("run_id")
            if action == "mission.create":
                checked = await self.validate(payload)
                mission = {
                    "id": new_id("mission"),
                    "revision": 1,
                    "created_at": utc_now(),
                    **checked,
                }
                mission["mission_id"] = mission["id"]
                result = mission
                records = [
                    ("missions", mission["id"], mission),
                    ("revisions", mission["id"] + ":1", mission),
                ]
            elif action == "mission.update":
                mission = await self.store.get("missions", payload["mission_id"])
                if revision != mission["revision"]:
                    raise AppError(
                        "revision_conflict", "If-Match must match the current mission revision", 412
                    )
                checked = await self.validate(payload["definition"])
                old, new = (
                    mission["definition"]["access"]["allow"],
                    checked["definition"]["access"]["allow"],
                )
                if (
                    set(new) - set(old)
                    or checked["definition"]["body"] != mission["definition"]["body"]
                ) and payload.get("confirm_access_change") is not True:
                    raise AppError(
                        "access_change_required",
                        "Explicitly confirm the changed body/access grant",
                        409,
                    )
                body_changed = checked["definition"]["body"] != mission["definition"]["body"]
                mission.update(**checked, revision=mission["revision"] + 1)
                mission["mission_id"] = mission["id"]
                result = mission
                records = [
                    ("missions", mission["id"], mission),
                    ("revisions", f"{mission['id']}:{mission['revision']}", mission),
                ]
                if body_changed:
                    for existing_run in await self.store.list("runs"):
                        if (
                            existing_run["mission_id"] == mission["id"]
                            and existing_run["lifecycle"] not in RUN_TERMINAL
                        ):
                            existing_run.update(
                                health="blocked", reason_code="revalidation_required", pending=[]
                            )
                            records.append(("runs", existing_run["id"], existing_run))
                            follow.append(("interrupt", existing_run["id"]))
            elif action == "mission.start":
                mission = await self.store.get("missions", payload["mission_id"])
                existing = next(
                    (
                        r
                        for r in await self.store.list("runs")
                        if r["mission_id"] == mission["id"] and r["lifecycle"] not in RUN_TERMINAL
                    ),
                    None,
                )
                if existing:
                    result = existing
                else:
                    definition = validate_definition(mission["definition"])
                    self._driver(definition)
                    port = await self._require_body(definition)
                    run_id = new_id("run")
                    result = {
                        "id": run_id,
                        "run_id": run_id,
                        "mission_id": mission["id"],
                        "revision": mission["revision"],
                        "name": definition.name,
                        "body_id": port.body_id,
                        "session_runtime_id": (await port.session.observe()).runtime_id,
                        "lifecycle": "active",
                        "activity": "waiting_event",
                        "health": "ok",
                        "reason_code": None,
                        "created_at": utc_now(),
                        "pending": [],
                        "inputs": [],
                        "active_turn_id": None,
                        "model_calls": 0,
                        "tool_calls": 0,
                        "turn_count": 0,
                        "dropped_events": 0,
                        "last_observation": None,
                        "last_result": None,
                        "turn_times": [],
                    }
                    if definition.trigger.kind == "manual" or definition.mode == "once":
                        result["pending"] = [
                            {"type": "manual", "at": utc_now(), "occurrence": new_id("manual")}
                        ]
                    records = [("runs", run_id, result)]
                    follow.append(("run", run_id))
            elif action in {"run.pause", "run.resume", "run.stop", "run.input"}:
                run = await self.store.get("runs", payload["run_id"])
                run_id = run["id"]
                if run["lifecycle"] in RUN_TERMINAL and action != "run.stop":
                    raise AppError("run_terminal", "This run is already terminal", 409)
                if action == "run.pause":
                    run.update(lifecycle="paused", activity="idle")
                    follow.append(("interrupt", run_id))
                elif action == "run.stop":
                    run.update(lifecycle="stopped", activity="idle", pending=[])
                    follow.extend([("interrupt", run_id), ("cancel_run", run_id)])
                elif action == "run.resume":
                    if run["active_turn_id"]:
                        raise AppError(
                            "turn_interrupting",
                            "Wait for the interrupted turn to finish before resuming",
                            409,
                        )
                    mission = await self.store.get("missions", run["mission_id"])
                    definition = validate_definition(mission["definition"])
                    port = await self._require_body(definition)
                    if run["reason_code"] == "revalidation_required":
                        run["pending"] = []
                    run.update(
                        lifecycle="active",
                        activity="waiting_event",
                        health="ok",
                        reason_code=None,
                        body_id=port.body_id,
                        revision=mission["revision"],
                        name=definition.name,
                        session_runtime_id=(await port.session.observe()).runtime_id,
                    )
                    follow.append(("run", run_id))
                else:
                    if len(run["inputs"]) >= 16:
                        raise AppError(
                            "input_queue_full",
                            "At most 16 instructions may await the next turn",
                            409,
                        )
                    if payload.get("target", "next_turn") != "next_turn":
                        raise AppError(
                            "unsupported_input_target",
                            "This driver accepts instructions for the next turn",
                        )
                    run["inputs"].append(text(payload.get("text"), "instruction"))
                    if payload.get("trigger", True) and len(run["pending"]) < 4:
                        run["pending"].append(
                            {"type": "manual", "at": utc_now(), "occurrence": new_id("input")}
                        )
                    follow.append(("wake", run_id))
                result = run
                records = [("runs", run_id, run)]
            elif action == "attachment.create":
                port = self.ports.get(payload.get("body_id", ""))
                if not port:
                    raise AppError("not_found", "Select a connected body", 404)
                allow = payload.get("allow", [])
                names = {c["name"] for c in (await port.describe())["capabilities"]}
                if (
                    not isinstance(allow, list)
                    or not allow
                    or not all(isinstance(c, str) and c in names for c in allow)
                ):
                    raise AppError(
                        "validation_error", "Choose a nonempty explicit capability scope"
                    )
                token = secrets.token_urlsafe(32)
                attachment: dict[str, Any] = {
                    "id": new_id("attachment"),
                    "name": text(payload.get("name"), "name", 160),
                    "body_id": port.body_id,
                    "allow": allow,
                    "status": "active",
                    "control": "external",
                    "created_at": utc_now(),
                    "token_hash": digest(token),
                }
                result = {k: v for k, v in attachment.items() if k != "token_hash"} | {
                    "token": token
                }
                records = [("attachments", attachment["id"], attachment)]
            elif action == "attachment.revoke":
                attachment = await self.store.get("attachments", payload["attachment_id"])
                attachment["status"] = "revoked"
                records = [("attachments", attachment["id"], attachment)]
                result = {k: v for k, v in attachment.items() if k != "token_hash"}
            elif action == "operation.create":
                result = await self._operation_intent(payload, actor)
                run_id = result.get("run_id")
                records = [("operations", result["id"], result)]
                if result["approval_required"]:
                    approval = {
                        "id": new_id("approval"),
                        "operation_id": result["id"],
                        "run_id": result["run_id"],
                        "revision": result["revision"],
                        "body_id": result["body_id"],
                        "session_runtime_id": result["session_runtime_id"],
                        "content_hash": digest([result["capability"], result["arguments"]]),
                        "expires_at": time.time() + 120,
                        "status": "pending",
                    }
                    result.update(dispatch="waiting_approval", approval_id=approval["id"])
                    records.append(("approvals", approval["id"], approval))
                else:
                    follow.append(("dispatch", result["id"]))
            elif action == "approval.decision":
                approval = await self.store.get("approvals", payload["approval_id"])
                op = await self.store.get("operations", approval["operation_id"])
                if approval["status"] != "pending" or op["lifecycle"] in OP_TERMINAL:
                    raise AppError("approval_invalid", "This approval is no longer pending", 409)
                run = await self.store.get("runs", approval["run_id"])
                mission = await self.store.get("missions", run["mission_id"])
                port = self.ports.get(op["body_id"])
                valid = (
                    approval["expires_at"] > time.time()
                    and run["lifecycle"] == "active"
                    and (not op.get("turn_id") or op["turn_id"] == run["active_turn_id"])
                    and mission["revision"] == approval["revision"]
                    and port
                    and port.available
                    and (await port.session.observe()).runtime_id == approval["session_runtime_id"]
                    and digest([op["capability"], op["arguments"]]) == approval["content_hash"]
                )
                decision = payload.get("allow")
                if not isinstance(decision, bool):
                    raise AppError(
                        "validation_error", "Approval decision requires a boolean allow field"
                    )
                if decision and not valid:
                    raise AppError(
                        "approval_invalid",
                        "Approval expired or its resource, revision, or content changed",
                        409,
                    )
                approval.update(status="approved" if decision else "denied", decided_at=utc_now())
                if decision:
                    op.update(approval_granted=True, dispatch="pending")
                    follow.append(("dispatch", op["id"]))
                else:
                    op.update(lifecycle="rejected", reason_code="APPROVAL_DENIED")
                records = [("approvals", approval["id"], approval), ("operations", op["id"], op)]
                result = approval
            elif action == "operation.cancel":
                result = await self.store.get("operations", payload["operation_id"])
                await self.authorize_operation(result, actor)
                result["cancel_requested"] = True
                if result["dispatch"] in {"pending", "waiting_approval"}:
                    result.update(lifecycle="canceled", reason_code="NOT_DISPATCHED")
                records = [("operations", result["id"], result)]
                if result.get("approval_id"):
                    approval = await self.store.get("approvals", result["approval_id"])
                    if approval["status"] == "pending":
                        approval.update(status="invalidated", reason_code="operation_canceled")
                        records.append(("approvals", approval["id"], approval))
                follow.append(("cancel", result["id"]))
            elif action == "alert.ack":
                result = await self.store.get("alerts", payload["alert_id"])
                result["acknowledged_at"] = utc_now()
                records = [("alerts", result["id"], result)]
            else:
                raise AppError("unsupported_command", "Unknown application command")
            public = {k: v for k, v in result.items() if k not in {"token", "token_hash"}}
            await self.store.commit(
                records, action, public, run_id=run_id, request=(actor, key, fingerprint, result)
            )
        for kind, identifier in follow:
            if kind in {"run", "wake"}:
                self._ensure_runner(identifier)
                self.wake[identifier].set()
            elif kind == "interrupt":
                if identifier in self.turn_tasks:
                    self.turn_tasks[identifier].cancel()
                if identifier in self.wake:
                    self.wake[identifier].set()
            elif kind == "dispatch":
                self.spawn(self._dispatch(identifier))
            elif kind == "cancel":
                self.spawn(self._cancel_operation(identifier))
            elif kind == "cancel_run":
                self.spawn(self._cancel_run(identifier))
        return result

    async def authorize_operation(self, op: dict[str, Any], actor: str) -> None:
        if actor != "owner":
            attachment = await self.store.get("attachments", actor)
            if attachment["status"] != "active" or op.get("attachment_id") != actor:
                raise AppError("forbidden", "Operation is outside the attachment scope", 403)

    async def _operation_intent(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        if actor != "owner" and payload.get("attachment_id") != actor:
            raise AppError("forbidden", "An external agent may use only its own attachment", 403)
        if bool(payload.get("run_id")) == bool(payload.get("attachment_id")):
            raise AppError(
                "validation_error", "An operation requires exactly one run or attachment"
            )
        revision = None
        approval_required = False
        if payload.get("run_id"):
            run = await self.store.get("runs", payload["run_id"])
            if payload.get("turn_id") and payload["turn_id"] != run["active_turn_id"]:
                raise AppError("turn_closed", "This turn may no longer admit operations", 409)
            if run["lifecycle"] != "active" or run["health"] == "blocked":
                raise AppError("admission_closed", "Mission admissions are closed", 409)
            mission = await self.store.get("revisions", f"{run['mission_id']}:{run['revision']}")
            definition = validate_definition(mission["definition"])
            body_id, allow = definition.body.selector, definition.access.allow
            revision = run["revision"]
            approval_required = payload.get("capability") in definition.access.approve
        else:
            attachment = await self.store.get("attachments", payload["attachment_id"])
            if attachment["status"] != "active":
                raise AppError("admission_closed", "Attachment access was revoked", 403)
            body_id, allow = attachment["body_id"], attachment["allow"]
        capability = text(payload.get("capability"), "capability", 200)
        if capability not in allow:
            raise AppError("forbidden", "Capability is outside the granted scope", 403)
        arguments = payload.get("arguments", {})
        if not isinstance(arguments, dict) or len(canonical(arguments).encode()) > 16384:
            raise AppError("validation_error", "Arguments must be a JSON object up to 16384 bytes")
        port = self.ports.get(body_id)
        if not port or not port.available:
            raise AppError("source_lost", "Body is unavailable", 409)
        view = await port.session.observe()
        spec = next((c for c in view.capabilities if c["name"] == capability), None)
        if not spec:
            raise AppError("capability_unavailable", "Capability is not exposed by this body", 409)
        if spec["physical_effects"]:
            uncertain = [
                o
                for o in await self.store.list("operations")
                if o["body_id"] == body_id
                and o["physical_effects"]
                and o["lifecycle"] == "indeterminate"
            ]
            if uncertain:
                raise AppError(
                    "resource_indeterminate",
                    "Reconcile the previous physical effect before new writes",
                    409,
                )
        return {
            "id": new_id("op"),
            "request_id": new_id("request"),
            "body_id": body_id,
            "run_id": payload.get("run_id"),
            "attachment_id": payload.get("attachment_id"),
            "turn_id": payload.get("turn_id"),
            "revision": revision,
            "capability": capability,
            "arguments": arguments,
            "session_runtime_id": view.runtime_id,
            "native_operation_id": None,
            "lifecycle": "accepted",
            "dispatch": "pending",
            "approval_required": approval_required,
            "approval_granted": False,
            "physical_effects": spec["physical_effects"],
            "effect_state": "none",
            "cancel_requested": False,
            "reason_code": None,
            "created_at": utc_now(),
            "evidence_ids": [],
        }

    async def _dispatch(self, identifier: str) -> None:
        async with self.lock:
            op = await self.store.get("operations", identifier)
            if op["lifecycle"] in OP_TERMINAL:
                return
            if op["cancel_requested"]:
                op.update(lifecycle="canceled", reason_code="NOT_DISPATCHED")
                await self._record("operations", op, "operation.canceled", op["run_id"])
                return
            try:
                checked = await self._operation_intent(op, "owner")
                if checked["approval_required"] and not op.get("approval_granted"):
                    raise AppError("approval_required", "Approval is required before dispatch", 409)
                if checked["session_runtime_id"] != op["session_runtime_id"]:
                    raise AppError("stale_runtime", "Body changed before dispatch", 409)
                if op.get("approval_granted"):
                    approval = await self.store.get("approvals", op["approval_id"])
                    run = await self.store.get("runs", op["run_id"])
                    mission = await self.store.get("missions", run["mission_id"])
                    if (
                        approval["expires_at"] <= time.time()
                        or mission["revision"] != approval["revision"]
                    ):
                        raise AppError("approval_invalid", "Approval changed before dispatch", 409)
            except AppError as error:
                op.update(lifecycle="rejected", reason_code=error.code)
                await self._record("operations", op, "operation.rejected", op["run_id"])
                return
            op["dispatch"] = "dispatching"
            await self._record("operations", op, "operation.dispatching", op["run_id"])
        port = self.ports[op["body_id"]]
        try:
            native = await port.session.act(
                op["capability"],
                op["arguments"],
                request_id=op["request_id"],
                expected_runtime_id=op["session_runtime_id"],
            )
            async with self.lock:
                op = await self.store.get("operations", identifier)
                op.update(native_operation_id=native.operation_id, dispatch="sent")
                await self._record("operations", op, "operation.admitted", op["run_id"])
            if op["cancel_requested"]:
                await port.session.cancel(native.operation_id)
            while not native.terminal:
                progress = await port.inspect(native.operation_id)
                if progress.get("lifecycle") in OP_TERMINAL:
                    native = await port.session.wait(native.operation_id, 0)
                    continue
                fields = {
                    k: progress[k]
                    for k in ("lifecycle", "motion_state", "effect_state", "phase", "delegation")
                    if k in progress
                }
                async with self.lock:
                    current = await self.store.get("operations", identifier)
                    if any(current.get(k) != v for k, v in fields.items()):
                        current.update(fields)
                        await self._record(
                            "operations", current, "operation.progress", current["run_id"]
                        )
                native = await port.session.wait(native.operation_id, 0.1)
            inspection = await port.inspect(native.operation_id)
            async with self.lock:
                op = await self.store.get("operations", identifier)
                await self._finish_operation(op, port, inspection)

        except asyncio.CancelledError:
            raise
        except Exception as error:
            async with self.lock:
                op = await self.store.get("operations", identifier)
                refused = isinstance(error, AdmissionError)
                op.update(
                    lifecycle="rejected" if refused else "indeterminate",
                    reason_code=error.reason_code
                    if isinstance(error, AdmissionError)
                    else "DISPATCH_UNCONFIRMED",
                    effect_state="unknown" if not refused and op["physical_effects"] else "none",
                )
                await self._record("operations", op, "operation.failed", op["run_id"])

    async def _finish_operation(
        self,
        op: dict[str, Any],
        port: EmbodimentPort,
        inspection: dict[str, Any],
        event: str = "operation.completed",
    ) -> None:
        """Publish a terminal outcome and its evidence in one transaction under the owner lock."""
        identifier = op["id"]
        evidence = {
            "id": new_id("ev"),
            "at": utc_now(),
            "operation_id": identifier,
            "origin": op["body_id"],
            "simulated": port.simulated,
            "content": inspection,
        }
        op.update(
            {
                k: public_content(v, evidence["id"])
                for k, v in inspection.items()
                if k != "operation_id"
            }
        )
        op["evidence_ids"] = [evidence["id"]]
        await self.store.commit(
            [("operations", identifier, op), ("evidence", evidence["id"], evidence)],
            event,
            op,
            run_id=op["run_id"],
            operation_id=identifier,
        )

    async def _cancel_operation(self, identifier: str) -> None:
        op = await self.store.get("operations", identifier)
        port = self.ports.get(op["body_id"])
        if port and op["native_operation_id"] and op["lifecycle"] not in OP_TERMINAL:
            with contextlib.suppress(AdmissionError, RuntimeError):
                await port.session.cancel(op["native_operation_id"])

    async def _cancel_run(self, run_id: str) -> None:
        for op in await self.store.list("operations"):
            if op["run_id"] == run_id and op["lifecycle"] not in OP_TERMINAL:
                await self.command("operation.cancel", {"operation_id": op["id"]}, new_id("stop"))

    def _ensure_runner(self, run_id: str) -> None:
        self.wake.setdefault(run_id, asyncio.Event())
        if run_id not in self.runners or self.runners[run_id].done():
            self.runners[run_id] = self.spawn(self._run(run_id))

    async def ingest(self, event: dict[str, Any]) -> None:
        """Accept a source cursor and bounded triggers in the same durable transaction."""
        async with self.lock:
            try:
                await self.store.check_capacity()
            except AppError:
                for run in await self.store.list("runs"):
                    if (
                        run["lifecycle"] not in RUN_TERMINAL
                        and run["reason_code"] != "storage_limit"
                    ):
                        run.update(health="blocked", reason_code="storage_limit", pending=[])
                        await self._record("runs", run, "mission.storage_limit", run["id"])
                        if run["id"] in self.turn_tasks:
                            self.turn_tasks[run["id"]].cancel()
                return
            for run in await self.store.list("runs"):
                if run["lifecycle"] in RUN_TERMINAL or run["body_id"] != event["source"]:
                    continue
                definition = validate_definition(
                    (await self.store.get("missions", run["mission_id"]))["definition"]
                )
                if definition.body.selector != event["source"]:
                    continue
                evidence_id = "ev_" + digest(event)
                observed = {**event, "evidence_id": evidence_id}
                prior_cursor = run.get("source_cursor")
                if (
                    prior_cursor
                    and prior_cursor["epoch"] == event["epoch"]
                    and event["cursor"] <= prior_cursor["cursor"]
                ):
                    continue
                run["last_observation"] = observed
                run["source_cursor"] = {"epoch": event["epoch"], "cursor": event["cursor"]}
                if (
                    event["type"] == "source.lost"
                    or event.get("age_ms", 0) > definition.observation.max_age_ms
                ):
                    run.update(
                        health="blocked",
                        pending=[],
                        reason_code="source_lost"
                        if event["type"] == "source.lost"
                        else "stale_observation",
                    )
                    if run["id"] in self.turn_tasks:
                        self.turn_tasks[run["id"]].cancel()
                    event_type = "mission.coverage_lost"
                elif event["type"] == "source.revalidated":
                    if definition.recovery.resume_after_revalidation and run.get("reason_code") in {
                        "source_lost",
                        "stale_observation",
                        "revalidation_required",
                    }:
                        run.update(health="ok", reason_code=None, activity="waiting_event")
                    event_type = "mission.revalidated"
                else:
                    event_type = "mission.observed"
                    if (
                        run["health"] != "blocked"
                        and run["lifecycle"] == "active"
                        and definition.trigger.kind == "event"
                        and definition.trigger.event == event["type"]
                    ):
                        pending = run["pending"]
                        if not any(e.get("occurrence") == event["occurrence"] for e in pending):
                            if len(pending) >= definition.trigger.max_pending:
                                run["dropped_events"] += 1
                                event_type = "mission.queue_overflow"
                            else:
                                pending.append(observed)
                evidence = {
                    "id": evidence_id,
                    "origin": event["source"],
                    "at": event["at"],
                    "simulated": event.get("simulated", False),
                    "content": event,
                }
                await self.store.commit(
                    [("runs", run["id"], run), ("evidence", evidence_id, evidence)],
                    event_type,
                    run,
                    run_id=run["id"],
                )
                self._ensure_runner(run["id"])
                self.wake[run["id"]].set()

    async def demo_step(self, action: str) -> dict[str, Any]:
        if not self.demo:
            raise AppError("demo_disabled", "The simulated body is disabled", 404)
        event = self.demo.step(action)
        await self.ingest(event)
        return event

    async def _run(self, run_id: str) -> None:
        while not self.closed:
            wake = self.wake[run_id]
            wake.clear()
            run = await self.store.get("runs", run_id)
            if run["lifecycle"] in RUN_TERMINAL:
                return
            definition = validate_definition(
                (await self.store.get("missions", run["mission_id"]))["definition"]
            )
            if run["lifecycle"] == "active" and run["health"] != "blocked" and run["pending"]:
                task = self.spawn(self._turn(run_id))
                self.turn_tasks[run_id] = task
                try:
                    await task
                except asyncio.CancelledError:
                    if self.closed:
                        return
                finally:
                    self.turn_tasks.pop(run_id, None)
                continue
            try:
                timeout = (
                    definition.trigger.interval_s
                    if definition.trigger.kind == "interval"
                    and run["lifecycle"] == "active"
                    and run["health"] != "blocked"
                    else None
                )
                await asyncio.wait_for(wake.wait(), timeout)
            except TimeoutError:
                async with self.lock:
                    run = await self.store.get("runs", run_id)
                    if run["lifecycle"] == "active" and run["health"] != "blocked":
                        run["pending"] = [
                            {"type": "interval", "at": utc_now(), "occurrence": new_id("interval")}
                        ]
                        await self._record("runs", run, "mission.interval", run_id)

    async def _turn(self, run_id: str) -> None:
        async with self.lock:
            run = await self.store.get("runs", run_id)
            if not run["pending"] or run["lifecycle"] != "active" or run["health"] == "blocked":
                return
            mission = await self.store.get("missions", run["mission_id"])
            definition = validate_definition(mission["definition"])
            now = time.time()
            run["turn_times"] = [stamp for stamp in run["turn_times"] if stamp > now - 3600]
            if len(run["turn_times"]) >= definition.limits.max_turns_per_hour:
                run.update(health="blocked", reason_code="turn_rate_limit")
                await self._record("runs", run, "mission.blocked", run_id)
                return
            observation = run["pending"].pop(0)
            inputs, run["inputs"] = run["inputs"], []
            turn = {
                "id": new_id("turn"),
                "run_id": run_id,
                "revision": mission["revision"],
                "instructions_hash": mission["instructions_hash"],
                "status": "running",
                "created_at": utc_now(),
                "observation": observation,
                "model_calls": 0,
                "tool_calls": 0,
            }
            run.update(
                revision=mission["revision"], active_turn_id=turn["id"], activity="reasoning"
            )
            run["turn_times"].append(now)
            await self.store.commit(
                [("runs", run_id, run), ("turns", turn["id"], turn)],
                "turn.started",
                turn,
                run_id=run_id,
                turn_id=turn["id"],
            )
        from entryplug_app.tools import ToolBroker

        context = TurnContext(run_id, turn["id"], definition, observation, inputs, None)  # type: ignore[arg-type]
        context.tools = ToolBroker(self, context)

        async def on_text(delta: str) -> None:
            self.transient(
                {
                    "type": "turn.text",
                    "run_id": run_id,
                    "turn_id": turn["id"],
                    "delta": delta[:2048],
                }
            )

        context.on_text = on_text
        status, message, reason = "completed", "", None
        try:
            await self._require_body(definition)
            if observation.get("source"):
                from datetime import datetime

                age = (
                    time.time()
                    - datetime.fromisoformat(observation["at"].replace("Z", "+00:00")).timestamp()
                ) * 1000
                if max(age, observation.get("age_ms", 0)) > definition.observation.max_age_ms:
                    raise AppError(
                        "stale_observation", "Queued observation expired before the turn", 409
                    )
            async with asyncio.timeout(definition.limits.turn_timeout_s):
                message = await self._driver(definition).turn(context)
        except asyncio.CancelledError:
            status, reason = "interrupted", "turn_interrupted"
        except TimeoutError:
            status, reason = "failed", "turn_timeout"
        except AppError as error:
            status, reason = "blocked", error.code
        except Exception:
            status, reason = "failed", "driver_failed"
        async with self.lock:
            run = await self.store.get("runs", run_id)
            turn.update(
                status=status,
                result=message,
                reason_code=reason,
                completed_at=utc_now(),
                model_calls=context.model_calls,
                tool_calls=context.tool_calls,
                usage=context.usage,
            )
            run.update(
                active_turn_id=None,
                activity="waiting_event" if run["lifecycle"] == "active" else "idle",
                last_result=message or reason,
                model_calls=run["model_calls"] + context.model_calls,
                tool_calls=run["tool_calls"] + context.tool_calls,
                turn_count=run["turn_count"] + 1,
            )
            if status in {"blocked", "failed"}:
                run.update(health="blocked", reason_code=reason)
            if definition.mode == "once" and status == "completed" and run["lifecycle"] == "active":
                run.update(lifecycle="completed", activity="idle")
            await self.store.commit(
                [("runs", run_id, run), ("turns", turn["id"], turn)],
                "turn.finished",
                turn,
                run_id=run_id,
                turn_id=turn["id"],
            )

            if status != "completed":
                for op in await self.store.list("operations"):
                    if (
                        op.get("turn_id") == turn["id"]
                        and op["dispatch"] in {"pending", "waiting_approval"}
                        and op["lifecycle"] not in OP_TERMINAL
                    ):
                        op.update(lifecycle="canceled", reason_code="TURN_CLOSED")
                        abandoned = [("operations", op["id"], op)]
                        if op.get("approval_id"):
                            approval = await self.store.get("approvals", op["approval_id"])
                            approval.update(status="invalidated", reason_code="turn_closed")
                            abandoned.append(("approvals", approval["id"], approval))
                        await self.store.commit(abandoned, "operation.abandoned", op, run_id=run_id)

    async def emit_alert(self, context: TurnContext, message: str) -> dict[str, Any]:
        occurrence = context.observation.get("occurrence", context.turn_id)
        identifier = "alert_" + digest([context.run_id, occurrence])
        async with self.lock:
            run = await self.store.get("runs", context.run_id)
            if (
                run["lifecycle"] != "active"
                or run["health"] == "blocked"
                or run["active_turn_id"] != context.turn_id
            ):
                raise AppError("admission_closed", "Turn may no longer emit alerts", 409)
            with contextlib.suppress(AppError):
                return await self.store.get("alerts", identifier)
            alert = {
                "id": identifier,
                "run_id": context.run_id,
                "turn_id": context.turn_id,
                "at": utc_now(),
                "message": text(message, "alert", 4096),
                "occurrence": occurrence,
                "acknowledged_at": None,
                "delivery": "delivered",
                "sink": "inbox",
                "evidence_ids": list(
                    dict.fromkeys(
                        context.evidence_ids
                        + (
                            [context.observation["evidence_id"]]
                            if context.observation.get("evidence_id")
                            else []
                        )
                    )
                ),
            }
            delivery = {
                "id": identifier,
                "alert_id": identifier,
                "sink": "inbox",
                "status": "delivered",
            }
            await self.store.commit(
                [("alerts", identifier, alert), ("deliveries", identifier, delivery)],
                "alert.created",
                alert,
                run_id=context.run_id,
                turn_id=context.turn_id,
            )
            return alert

    async def close(self) -> None:
        if not self.started or self.closed:
            return
        self.closed = True
        for task in list(self.turn_tasks.values()):
            task.cancel()
        for wake in self.wake.values():
            wake.set()
        await asyncio.gather(*self.turn_tasks.values(), return_exceptions=True)
        for port in self.ports.values():
            try:
                await port.close()
            except Exception:
                await self.store.commit([], "service.close_unconfirmed", {"body_id": port.body_id})
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        async with self.lock:
            for op in await self.store.list("operations"):
                if op["lifecycle"] not in OP_TERMINAL:
                    closing_port = self.ports.get(op["body_id"])
                    inspection = None
                    if closing_port and op.get("native_operation_id"):
                        with contextlib.suppress(Exception):
                            inspection = await closing_port.inspect(op["native_operation_id"])
                    if closing_port and inspection and inspection["lifecycle"] in OP_TERMINAL:
                        await self._finish_operation(
                            op, closing_port, inspection, "operation.closed"
                        )
                        continue
                    elif op["dispatch"] in {"pending", "waiting_approval"}:
                        op.update(lifecycle="canceled", reason_code="NOT_DISPATCHED")
                    else:
                        op.update(
                            lifecycle="indeterminate",
                            reason_code="SHUTDOWN_UNCONFIRMED",
                            effect_state="unknown" if op["physical_effects"] else "none",
                        )
                    await self._record("operations", op, "operation.closed", op["run_id"])
            await self.store.commit([], "service.stopped", {})
        await self.store.close()
        self.owner.release()
