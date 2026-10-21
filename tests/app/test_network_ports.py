"""Native network authority and effect semantics, without optional SDK imports."""

import asyncio
import copy
import json

import pytest

from entryplug.core.operation import AdmissionError, EffectState, Lifecycle
from entryplug_app.contracts import AppError
from entryplug_app.network_port import network_port
from entryplug_app.network_profile import validate_network


def config(kind="mqtt"):
    profile = {
        "observations": [
            {
                "name": "room.temperature",
                "key": "room/temperature",
                "description": "Temperature in Celsius",
                "max_age_s": 1,
            }
        ],
        "actions": [
            {
                "name": "lamp.set",
                "key": "lamp/instance-1/set",
                "description": "Set lamp brightness from 0 to 0.75",
                "timeout_s": 0.2,
                "input_schema": {
                    "type": "object",
                    "properties": {"level": {"type": "number", "minimum": 0, "maximum": 0.75}},
                    "required": ["level"],
                    "additionalProperties": False,
                },
            }
        ],
    }
    if kind == "mqtt":
        profile["reply_key"] = "entryplug/client-1/reply"
    return {
        "id": "network",
        "name": "Room",
        "kind": kind,
        "url": "mqtt://127.0.0.1:1883" if kind == "mqtt" else "tcp/127.0.0.1:7447",
        "profile": profile,
    }


class Transport:
    def __init__(self, config, credentials, receive):
        self.receive = receive
        self.requests = []
        self.closed = False
        self.mode = "applied"
        self.started = asyncio.Event()

    async def open(self):
        pass

    def check(self):
        if self.closed:
            raise ConnectionError("closed")

    async def request(self, key, payload, request_id, timeout):
        self.requests.append((key, json.loads(payload)))
        self.started.set()
        if self.mode == "lost":
            await asyncio.Event().wait()
        return json.dumps(
            {
                "request_id": "wrong" if self.mode == "wrong" else request_id,
                "status": self.mode if self.mode != "wrong" else "applied",
                "result": {"level": 0.5},
            }
        ).encode()

    async def close(self):
        self.closed = True


async def connected(kind="mqtt"):
    transports = []

    def factory(*args):
        transports.append(Transport(*args))
        return transports[-1]

    port = await network_port(config(kind), None, transport_factory=factory)
    return port, transports[0]


async def terminal(session, op):
    async with asyncio.timeout(2):
        while not op.terminal:
            op = await session.wait(op, 0.05)
    return op


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["mqtt", "zenoh"])
async def test_named_scoped_actions_use_device_reports_and_deduplicate(kind):
    port, transport = await connected(kind)
    try:
        description = await port.describe()
        assert description["protocol"] == kind
        assert description["network_scope"]["actions"][0]["capability"] == "lamp.set"
        with pytest.raises(AdmissionError):
            await port.session.act("publish", {"key": "private/command"})
        for args in ({"level": 1}, {"level": True}, {"level": 0.5, "key": "other"}, {}):
            with pytest.raises(AdmissionError):
                await port.session.act("lamp.set", args)
        assert transport.requests == []
        op = await port.session.act("lamp.set", {"level": 0.5}, request_id="same")
        result = await terminal(port.session, op)
        assert result.lifecycle == Lifecycle.SUCCEEDED
        assert result.effect_state == EffectState.REPORTED
        assert (
            await port.session.act("lamp.set", {"level": 0.5}, request_id="same")
        ).operation_id == op.operation_id
        assert len(transport.requests) == 1
        assert transport.requests[0][0] == "lamp/instance-1/set"
    finally:
        await port.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["wrong", "lost", "cancel"])
async def test_lost_wrong_or_canceled_reply_inhibits_writes_but_keeps_reads(mode):
    port, transport = await connected()
    try:
        transport.mode = "lost" if mode == "cancel" else mode
        op = await port.session.act("lamp.set", {"level": 0.5})
        await transport.started.wait()
        if mode == "cancel":
            await port.session.cancel(op)
        result = await terminal(port.session, op)
        assert result.lifecycle == Lifecycle.INDETERMINATE
        assert result.effect_state == EffectState.UNKNOWN
        with pytest.raises(AdmissionError) as refusal:
            await port.session.act("lamp.set", {"level": 0.5})
        assert refusal.value.reason_code == "EFFECT_INHIBITED"
        transport.receive("room/temperature", b'{"celsius":22}')
        read = await terminal(port.session, await port.session.act("room.temperature", {}))
        assert read.lifecycle == Lifecycle.SUCCEEDED
        assert read.result["source_age_s"] is None
        assert len(transport.requests) == 1
    finally:
        await port.close()


@pytest.mark.asyncio
async def test_retained_samples_do_not_establish_current_readiness(monkeypatch):
    port, transport = await connected()
    try:
        for payload, metadata, reason in [
            (b"20", {"retained": True}, "RETAINED_UNVERIFIED"),
            (b"", {"deleted": True}, "SAMPLE_DELETED"),
            (b"x" * 9000, {}, "PAYLOAD_TOO_LARGE"),
        ]:
            transport.receive("room/temperature", payload, **metadata)
            result = await terminal(port.session, await port.session.act("room.temperature", {}))
            assert result.lifecycle == Lifecycle.FAILED and result.reason_code == reason
            assert result.result["value"] is None
        clock = [10]
        monkeypatch.setattr("entryplug_bridges.zenoh.time.monotonic", lambda: clock[0])
        transport.receive("room/temperature", b"21")
        # Check the cache directly, without replacing asyncio's own monotonic clock.
        clock[0] = 12
        monkeypatch.undo()
        result = await terminal(port.session, await port.session.act("room.temperature", {}))
        assert result.reason_code == "STALE"
    finally:
        await port.close()


@pytest.mark.asyncio
async def test_device_rejection_is_known_and_allows_a_later_request():
    port, transport = await connected()
    try:
        transport.mode = "rejected"
        result = await terminal(port.session, await port.session.act("lamp.set", {"level": 0.5}))
        assert result.lifecycle == Lifecycle.FAILED and result.effect_state == EffectState.NONE
        transport.mode = "applied"
        result = await terminal(port.session, await port.session.act("lamp.set", {"level": 0.5}))
        assert result.lifecycle == Lifecycle.SUCCEEDED
    finally:
        await port.close()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c.update(url="mqtt://user:secret@host"),
        lambda c: c.update(url="mqtt://host:0"),
        lambda c: c["profile"]["actions"][0].update(key="lamp/+/set"),
        lambda c: c["profile"]["actions"][0].update(name="room.temperature"),
        lambda c: c["profile"].update(reply_key="lamp/instance-1/set"),
        lambda c: c["profile"]["actions"][0]["input_schema"].update(additionalProperties=True),
        lambda c: c["profile"]["actions"][0]["input_schema"]["properties"]["level"].update(
            exclusiveMaximum=0.5
        ),
        lambda c: c["profile"]["actions"][0].update(timeout_s=float("nan")),
    ],
)
def test_profiles_fail_closed_before_connection(mutate):
    value = copy.deepcopy(config())
    mutate(value)
    with pytest.raises(AppError):
        validate_network(value)


@pytest.mark.asyncio
async def test_duplicate_action_route_cannot_acquire_a_second_local_ledger(tmp_path, monkeypatch):
    from entryplug_app.connections import Connections
    from entryplug_app.service import ApplicationService
    from entryplug_app.workspace import Workspace

    service = ApplicationService(Workspace.resolve(str(tmp_path)), demo=False)
    await service.open()
    try:
        connections = Connections(service)
        monkeypatch.setattr(connections, "_start", lambda _: True)
        payload = {k: v for k, v in config().items() if k != "id"}
        first = await connections.create(payload)
        with pytest.raises(AppError) as refused:
            await connections.create({**payload, "url": "mqtt://127.0.0.1/"})
        assert refused.value.code == "resource_already_bound"
        await connections.disconnect(first["id"])
        second = await connections.create(payload)
        assert second["id"] != first["id"]
        with pytest.raises(AppError) as refused:
            await connections.retry(first["id"])
        assert refused.value.code == "resource_already_bound"
        # An unresolved old operation cannot be hidden by disconnecting/recreating.
        await connections.disconnect(second["id"])
        unknown = {
            "id": "unknown-write",
            "body_id": second["id"],
            "physical_effects": True,
            "lifecycle": "indeterminate",
            "effect_state": "unknown",
        }
        await service.store.commit([("operations", unknown["id"], unknown)], "fixture.unknown", {})
        with pytest.raises(AppError) as refused:
            await connections.create(payload)
        assert refused.value.code == "resource_already_bound"
        assert len(await service.store.list("connections")) == 2
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_explicit_missing_native_credential_never_attempts_anonymous_connection(
    tmp_path, monkeypatch
):
    from entryplug_app.connections import Connections
    from entryplug_app.service import ApplicationService
    from entryplug_app.workspace import Workspace

    monkeypatch.delenv("ENTRYPLUG_TEST_MISSING_CREDENTIAL", raising=False)
    calls = []

    async def connect(*args):
        calls.append(args)
        raise RuntimeError("must not connect")

    monkeypatch.setattr("entryplug_app.network_port.network_port", connect)
    service = ApplicationService(Workspace.resolve(str(tmp_path)), demo=False)
    await service.open()
    try:
        payload = {k: v for k, v in config().items() if k != "id"}
        payload["token_env"] = "ENTRYPLUG_TEST_MISSING_CREDENTIAL"
        result = await Connections(service).create(payload)
        async with asyncio.timeout(2):
            while True:
                state = await service.store.get("connections", result["id"])
                if state["status"] == "unverified":
                    break
                await asyncio.sleep(0.01)
        assert state["reason_code"] == "credentials_missing"
        assert calls == []
    finally:
        await service.close()
