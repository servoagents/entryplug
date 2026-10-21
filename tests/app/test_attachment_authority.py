"""External agents share the resident owner's approval and dispatch authority."""

import asyncio

import pytest

from entryplug.core.operation import (
    CapabilitySpec,
    EffectState,
    Lifecycle,
    MotionState,
    OperationHost,
    OperationResult,
)
from entryplug.harness.session import Session
from entryplug_app.contracts import AppError
from entryplug_app.ports import EmbodimentPort
from entryplug_app.service import ApplicationService
from entryplug_app.workspace import Workspace


async def fixture(tmp_path):
    writes = []

    async def run(context, arguments):
        writes.append(dict(arguments))
        return OperationResult(
            Lifecycle.SUCCEEDED, MotionState.IDLE, effect_state=EffectState.REPORTED
        )

    service = ApplicationService(Workspace.resolve(str(tmp_path)))
    await service.open()
    service.ports["lamp"] = EmbodimentPort(
        "lamp",
        "Scoped lamp",
        Session(
            OperationHost(
                [
                    CapabilitySpec(
                        "lamp.set",
                        "1",
                        "Set lamp",
                        False,
                        1,
                        0.1,
                        lambda args: args,
                        run,
                        physical_effects=True,
                    )
                ]
            ),
            owns_runtime=True,
        ),
    )
    return service, writes


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [False, True])
async def test_external_physical_grant_waits_for_owner_approval(tmp_path, explicit):
    service, writes = await fixture(tmp_path)
    try:
        scope = {"approve": ["lamp.set"]} if explicit else {}
        grant = await service.command(
            "attachment.create",
            {"name": "Agent", "body_id": "lamp", "allow": ["lamp.set"], **scope},
            "grant",
        )
        op = await service.command(
            "operation.create",
            {"attachment_id": grant["id"], "capability": "lamp.set"},
            "act",
            actor=grant["id"],
        )
        await asyncio.sleep(0.02)
        assert writes == []
        assert op["dispatch"] == "waiting_approval"
        with pytest.raises(AppError):
            await service.command(
                "approval.decision",
                {"approval_id": op["approval_id"], "allow": True},
                "self-approve",
                actor=grant["id"],
            )
        await service.command(
            "approval.decision", {"approval_id": op["approval_id"], "allow": True}, "owner-approve"
        )
        async with asyncio.timeout(2):
            while not writes:
                await asyncio.sleep(0.01)
        assert writes == [{}]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_revocation_invalidates_pending_approval(tmp_path):
    service, writes = await fixture(tmp_path)
    try:
        grant = await service.command(
            "attachment.create",
            {"name": "Agent", "body_id": "lamp", "allow": ["lamp.set"], "approve": ["lamp.set"]},
            "grant",
        )
        op = await service.command(
            "operation.create",
            {"attachment_id": grant["id"], "capability": "lamp.set"},
            "act",
            actor=grant["id"],
        )
        assert op["dispatch"] == "waiting_approval"
        await service.command("attachment.revoke", {"attachment_id": grant["id"]}, "revoke")
        with pytest.raises(AppError):
            await service.command(
                "approval.decision", {"approval_id": op["approval_id"], "allow": True}, "approve"
            )
        assert writes == []
        assert (await service.store.get("operations", op["id"]))["lifecycle"] == "rejected"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_owner_can_delegate_actions_without_per_call_approval(tmp_path):
    service, writes = await fixture(tmp_path)
    try:
        grant = await service.command(
            "attachment.create",
            {"name": "Agent", "body_id": "lamp", "allow": ["lamp.set"], "approve": []},
            "grant",
        )
        op = await service.command(
            "operation.create",
            {"attachment_id": grant["id"], "capability": "lamp.set"},
            "act",
            actor=grant["id"],
        )
        assert not op["approval_required"]
        async with asyncio.timeout(2):
            while not writes:
                await asyncio.sleep(0.01)
        assert writes == [{}]
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["expired", "runtime", "arguments"])
async def test_approval_cannot_survive_changed_admission_context(tmp_path, change):
    service, writes = await fixture(tmp_path)
    try:
        grant = await service.command(
            "attachment.create",
            {"name": "Agent", "body_id": "lamp", "allow": ["lamp.set"]},
            "grant",
        )
        op = await service.command(
            "operation.create",
            {"attachment_id": grant["id"], "capability": "lamp.set"},
            "act",
            actor=grant["id"],
        )
        approval = await service.store.get("approvals", op["approval_id"])
        if change == "expired":
            approval["expires_at"] = 0
        elif change == "runtime":
            approval["session_runtime_id"] = "retired-runtime"
        else:
            op["arguments"] = {"changed": True}
        await service.store.commit(
            [("operations", op["id"], op), ("approvals", approval["id"], approval)],
            "fixture.changed",
            {},
        )
        with pytest.raises(AppError) as refused:
            await service.command(
                "approval.decision", {"approval_id": approval["id"], "allow": True}, "approve"
            )
        assert refused.value.code == "approval_invalid"
        assert not writes
    finally:
        await service.close()
