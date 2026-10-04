"""Mission authority over the real portable task; ports here are labelled test fixtures."""

import asyncio
import time

import pytest

from entryplug.core.evidence import json_object
from entryplug.core.operation import OperationHost
from entryplug.embodiment.inspection import (
    CameraFrame,
    LightReport,
    MarkerMeasurement,
    inspection_spec,
)
from entryplug.harness.session import Session
from entryplug_app.contracts import AppError, validate_definition
from entryplug_app.inspection import inspection_mission
from entryplug_app.ports import EmbodimentPort
from entryplug_app.service import ApplicationService
from entryplug_app.workspace import Workspace


class Resources:
    worker_id = "test-worker"
    program_id = "test-marker"
    entity_id = "test-light"

    def __init__(self, *, dark=False):
        self.sequence = 0
        self.level = 0.0
        self.writes = []
        self.dark = dark

    async def capture(self):
        self.sequence += 1
        return CameraFrame(
            "test-camera",
            "test-lineage",
            str(self.sequence),
            self.sequence,
            time.monotonic(),
            4,
            4,
            bytes(48),
        )

    async def detect(self, frame):
        return MarkerMeasurement(
            frame.source_id,
            frame.sample_id,
            self.worker_id,
            self.program_id,
            1.0,
            2.0,
            0.1 if self.dark or self.level == 0 else 0.9,
        )

    async def set_brightness(self, level):
        self.writes.append(level)
        self.level = level
        return LightReport(self.entity_id, level, True)


async def wait_for(service, kind, predicate):
    async with asyncio.timeout(3):
        while True:
            values = await service.store.list(kind)
            found = next((v for v in values if predicate(v)), None)
            if found:
                return found
            await asyncio.sleep(0.01)


def make_service(tmp_path, resources):
    session = Session(
        OperationHost([inspection_spec(resources, resources, light=resources)]), owns_runtime=True
    )
    port = EmbodimentPort("test-inspection", "Test inspection", session, simulated=True)
    return ApplicationService(Workspace.resolve(str(tmp_path)), ports=[port], demo=False), session


@pytest.mark.asyncio
async def test_inspection_requires_approval_then_reuses_body_without_model_or_extra_write(tmp_path):
    resources = Resources()
    service, session = make_service(tmp_path, resources)
    await service.open()
    try:
        mission = await service.command(
            "mission.create", inspection_mission("test-inspection"), "create"
        )
        first_samples = None
        for index in range(2):
            run = await service.command(
                "mission.start", {"mission_id": mission["id"]}, f"start-{index}"
            )
            approval = await wait_for(service, "approvals", lambda v: v["status"] == "pending")
            assert resources.writes == ([] if index == 0 else [0.25])
            await service.command(
                "approval.decision",
                {"approval_id": approval["id"], "allow": True},
                f"approve-{index}",
            )
            finished = await wait_for(
                service,
                "runs",
                lambda v, run=run: v["id"] == run["id"] and v["lifecycle"] == "completed",
            )
            assert finished["model_calls"] == 0 and finished["tool_calls"] == 1
            operation = await wait_for(
                service,
                "operations",
                lambda v, run=run: v["run_id"] == run["id"] and v["lifecycle"] == "succeeded",
            )
            native = await session.inspect(operation["native_operation_id"], "result")
            assert operation["result"] == json_object(native["result"], "result")
            assert operation["evidence_ids"]
            samples = set(operation["result"]["sample_ids"])
            if first_samples is not None:
                assert not samples & first_samples
            first_samples = samples
        assert resources.writes == [0.25]
        assert len((await session.observe()).operations) == 2
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_unobservable_target_blocks_mission_without_claiming_completion(tmp_path):
    resources = Resources(dark=True)
    service, _ = make_service(tmp_path, resources)
    await service.open()
    try:
        mission = await service.command(
            "mission.create", inspection_mission("test-inspection", approve=False), "create"
        )
        await service.command("mission.start", {"mission_id": mission["id"]}, "start")
        run = await wait_for(service, "runs", lambda v: v["turn_count"] == 1)
        assert run["lifecycle"] != "completed"
        assert run["health"] == "blocked"
        assert run["reason_code"] == "OBSERVATION_UNAVAILABLE"
        assert resources.writes == [0.25, 0.5, 0.75]
    finally:
        await service.close()


def test_inspection_mission_cannot_expand_into_unbounded_automation():
    value = inspection_mission("test-inspection")
    for key, change in [
        ("mode", "watch"),
        ("trigger", {"kind": "interval"}),
        ("access", {"allow": ["inspect_target", "alerts.emit"]}),
    ]:
        with pytest.raises(AppError):
            validate_definition({**value, key: change})


@pytest.mark.asyncio
async def test_denied_inspection_never_dispatches_to_native_body(tmp_path):
    resources = Resources()
    service, session = make_service(tmp_path, resources)
    await service.open()
    try:
        mission = await service.command(
            "mission.create", inspection_mission("test-inspection"), "create"
        )
        await service.command("mission.start", {"mission_id": mission["id"]}, "start")
        approval = await wait_for(service, "approvals", lambda v: v["status"] == "pending")
        await service.command(
            "approval.decision", {"approval_id": approval["id"], "allow": False}, "deny"
        )
        run = await wait_for(service, "runs", lambda v: v["turn_count"] == 1)
        assert run["reason_code"] == "APPROVAL_DENIED"
        assert resources.writes == [] and resources.sequence == 0
        assert (await session.observe()).operations == ()
    finally:
        await service.close()


def test_cli_template_is_valid_without_connecting_to_a_service(capsys):
    import json

    from entryplug.cli import main

    assert main(["mission", "template", "--body", "borrowed-light", "--json"]) == 0
    value = validate_definition(json.loads(capsys.readouterr().out))
    assert value.agent.driver == "inspection"
    assert value.access.approve == ["inspect_target"]
    assert value.body.selector == "borrowed-light"
