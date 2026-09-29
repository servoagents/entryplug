#!/usr/bin/env python3
"""Enroll only a fresh, disposable Home Assistant test container via its APIs.

This script is mounted read-only into the owned HA container. Its output is
scrubbed; credentials exist only in memory and a mode-0600 private token file.
It is fixture authority, never an Entryplug capability or agent tool.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

import aiohttp

BASE = "http://127.0.0.1:8123"
CLIENT_ID = f"{BASE}/"
REQUIRED_STEPS = frozenset({"user", "core_config", "integration", "analytics"})


class BootstrapIncompatible(RuntimeError):
    """The pinned frontend flow no longer matches the qualified contract."""


async def request(
    session: aiohttp.ClientSession,
    method: str,
    path: str,
    *,
    token: str | None = None,
    json_body: dict[str, Any] | None = None,
    form: dict[str, str] | None = None,
) -> Any:
    headers = {"Authorization": f"Bearer {token}"} if token else None
    async with session.request(
        method, BASE + path, headers=headers, json=json_body, data=form
    ) as response:
        if response.status < 200 or response.status >= 300:
            raise BootstrapIncompatible(f"{method} {path} returned HTTP {response.status}")
        try:
            return await response.json()
        except (aiohttp.ContentTypeError, json.JSONDecodeError) as error:
            raise BootstrapIncompatible(f"{method} {path} returned non-JSON") from error


async def wait_for_onboarding(session: aiohttp.ClientSession) -> list[dict[str, Any]]:
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        try:
            result = await request(session, "GET", "/api/onboarding")
            if isinstance(result, list):
                return result
        except (aiohttp.ClientError, TimeoutError, BootstrapIncompatible):
            pass
        await asyncio.sleep(1)
    raise BootstrapIncompatible("fresh Home Assistant onboarding did not become ready")


def check_fresh(steps: list[dict[str, Any]]) -> None:
    if (
        not all(isinstance(step, dict) for step in steps)
        or {step.get("step") for step in steps} != REQUIRED_STEPS
        or any(step.get("done") is not False for step in steps)
    ):
        raise BootstrapIncompatible("Home Assistant is not an expected fresh fixture")


async def long_lived_token(session: aiohttp.ClientSession, access_token: str) -> str:
    async with session.ws_connect(BASE.replace("http", "ws") + "/api/websocket") as socket:
        required = await socket.receive_json()
        if required.get("type") != "auth_required":
            raise BootstrapIncompatible("WebSocket auth handshake changed")
        await socket.send_json({"type": "auth", "access_token": access_token})
        authenticated = await socket.receive_json()
        if authenticated.get("type") != "auth_ok":
            raise BootstrapIncompatible("WebSocket authentication failed")
        await socket.send_json(
            {
                "id": 1,
                "type": "auth/long_lived_access_token",
                "client_name": "Entryplug disposable scenario",
                "lifespan": 1,
            }
        )
        result = await socket.receive_json()
        token = result.get("result")
        if result.get("id") != 1 or result.get("success") is not True or not isinstance(token, str):
            raise BootstrapIncompatible("long-lived token command failed")
        return token


async def configure_mqtt(session: aiohttp.ClientSession, token: str) -> None:
    flow = await request(
        session,
        "POST",
        "/api/config/config_entries/flow",
        token=token,
        json_body={"handler": "mqtt"},
    )
    if flow.get("type") != "form" or flow.get("step_id") != "broker":
        raise BootstrapIncompatible("MQTT config-entry start changed")
    flow_id = flow.get("flow_id")
    if not isinstance(flow_id, str):
        raise BootstrapIncompatible("MQTT flow has no ID")
    result = await request(
        session,
        "POST",
        f"/api/config/config_entries/flow/{flow_id}",
        token=token,
        json_body={
            "broker": "broker",
            "port": 1883,
            "protocol": "3.1.1",
            "other_settings": {
                "set_client_cert": False,
                "set_ca_cert": "off",
                "transport": "tcp",
            },
        },
    )
    if result.get("type") != "create_entry" or result.get("handler") != "mqtt":
        raise BootstrapIncompatible(
            f"MQTT flow ended as {result.get('type')} with errors {result.get('errors')}"
        )


async def configure_analytics(session: aiohttp.ClientSession, token: str) -> None:
    preferences = {
        "base": False,
        "snapshots": False,
        "diagnostics": False,
        "statistics": False,
        "usage": False,
    }
    async with session.ws_connect(BASE.replace("http", "ws") + "/api/websocket") as socket:
        if (await socket.receive_json()).get("type") != "auth_required":
            raise BootstrapIncompatible("analytics WebSocket handshake changed")
        await socket.send_json({"type": "auth", "access_token": token})
        if (await socket.receive_json()).get("type") != "auth_ok":
            raise BootstrapIncompatible("analytics WebSocket authentication failed")
        deadline = time.monotonic() + 30
        query_id = 1
        while True:
            await socket.send_json({"id": query_id, "type": "analytics"})
            readiness = await socket.receive_json()
            if readiness.get("id") != query_id:
                raise BootstrapIncompatible("analytics readiness reply changed")
            if readiness.get("success") is True:
                break
            code = readiness.get("error", {}).get("code")
            if code not in {"not_found", "unknown_command"} or time.monotonic() >= deadline:
                raise BootstrapIncompatible(f"analytics was unavailable: {code}")
            query_id += 1
            await asyncio.sleep(0.5)
        write_id = query_id + 1
        await socket.send_json(
            {"id": write_id, "type": "analytics/preferences", "preferences": preferences}
        )
        result = await socket.receive_json()
        if result.get("id") != write_id or result.get("success") is not True:
            code = result.get("error", {}).get("code")
            raise BootstrapIncompatible(f"analytics opt-out was rejected: {code}")
        await socket.send_json({"id": write_id + 1, "type": "analytics"})
        checked = await socket.receive_json()
        saved = checked.get("result", {}).get("preferences")
        if (
            checked.get("id") != write_id + 1
            or checked.get("success") is not True
            or saved != preferences
        ):
            raise BootstrapIncompatible("analytics opt-out was not confirmed")


def save_token(directory: Path, token: str) -> None:
    path = directory / "ha-token"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(token)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


async def bootstrap(directory: Path, expected_version: str) -> dict[str, object]:
    details = directory.stat()
    if details.st_uid != os.geteuid() or details.st_mode & 0o777 != 0o700:
        raise BootstrapIncompatible("private fixture directory must be owned and mode 0700")
    if (directory / "ha-token").exists():
        raise BootstrapIncompatible("fixture token path already exists")
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        steps = await wait_for_onboarding(session)
        check_fresh(steps)
        async with session.ws_connect(BASE.replace("http", "ws") + "/api/websocket") as ws:
            welcome = await ws.receive_json()
            if welcome.get("ha_version") != expected_version:
                raise BootstrapIncompatible("Home Assistant version is not the pinned version")
        username = "entryplug-" + secrets.token_hex(8)
        password = secrets.token_urlsafe(32)
        created = await request(
            session,
            "POST",
            "/api/onboarding/users",
            json_body={
                "name": "Entryplug disposable fixture",
                "username": username,
                "password": password,
                "client_id": CLIENT_ID,
                "language": "en",
            },
        )
        auth_code = created.get("auth_code")
        if not isinstance(auth_code, str):
            raise BootstrapIncompatible("onboarding did not return an authorization code")
        exchanged = await request(
            session,
            "POST",
            "/auth/token",
            form={"grant_type": "authorization_code", "code": auth_code, "client_id": CLIENT_ID},
        )
        short_token, refresh_token = exchanged.get("access_token"), exchanged.get("refresh_token")
        if not isinstance(short_token, str) or not isinstance(refresh_token, str):
            raise BootstrapIncompatible("authorization-code exchange changed")
        try:
            await request(session, "POST", "/api/onboarding/core_config", token=short_token)
            await request(
                session,
                "POST",
                "/api/onboarding/integration",
                token=short_token,
                json_body={"client_id": CLIENT_ID, "redirect_uri": CLIENT_ID},
            )
            await configure_analytics(session, short_token)
            await request(session, "POST", "/api/onboarding/analytics", token=short_token)
            completed = await request(session, "GET", "/api/onboarding")
            if not isinstance(completed, list) or not all(
                step.get("done") is True for step in completed
            ):
                raise BootstrapIncompatible("onboarding steps did not complete")
            await configure_mqtt(session, short_token)
            token = await long_lived_token(session, short_token)
            save_token(directory, token)
        finally:
            # This revokes the temporary authorization-code exchange. The
            # separate one-day fixture token remains valid until volume removal.
            async with session.post(
                BASE + "/auth/revoke", data={"token": refresh_token}
            ) as response:
                if response.status != 200:
                    raise BootstrapIncompatible("temporary refresh token revocation failed")
    return {"status": "passed", "home_assistant_version": expected_version, "mqtt": "configured"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--private-dir", type=Path, default=Path("/fixture-private"))
    parser.add_argument("--expected-version", default="2026.9.3")
    args = parser.parse_args()
    try:
        result = asyncio.run(bootstrap(args.private_dir, args.expected_version))
    except Exception as error:
        result = {"status": "failed", "reason": "BOOTSTRAP_INCOMPATIBLE", "stage": str(error)}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
