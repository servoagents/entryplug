"""No real accounts: use Authlib's real client/JWT validation against HTTP fixtures."""

import asyncio
import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import httpx2
import pytest
from authlib.integrations.httpx_client import AsyncOAuth2Client
from authlib.jose import JsonWebKey, JsonWebToken

from entryplug_agents.auth import ISSUER, SCOPE, ProviderProfiles
from entryplug_app.contracts import AppError
from entryplug_app.service import ApplicationService
from entryplug_app.workspace import Workspace


@pytest.mark.asyncio
async def test_pkce_identity_atomic_rotation_and_logout(tmp_path, monkeypatch):
    service = ApplicationService(Workspace.resolve(str(tmp_path)))
    await service.open()
    profiles = ProviderProfiles(service, "http://127.0.0.1:8765")
    await profiles.initialize()
    key = JsonWebKey.generate_key("RSA", 2048, {"kid": "fixture"}, is_private=True)
    nonce, refreshes = "", []
    metadata = {
        "issuer": ISSUER,
        "authorization_endpoint": ISSUER + "/api/accounts/authorize",
        "token_endpoint": ISSUER + "/api/accounts/oauth/token",
        "jwks_uri": ISSUER + "/.well-known/jwks.json",
        "revocation_endpoint": ISSUER + "/revoke",
    }

    async def respond(request):
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json=metadata)
        if request.url.path.endswith("jwks.json"):
            return httpx.Response(200, json={"keys": [key.as_dict(is_private=False)]})
        if request.url.path == "/revoke":
            assert b"refresh-rotated" in request.content
            return httpx.Response(200)
        fields = parse_qs(request.content.decode())
        assert fields["client_id"] == ["oaiapp_fixture"]
        if fields["grant_type"] == ["refresh_token"]:
            refreshes.append(fields["refresh_token"])
            await asyncio.sleep(0.02)
            return httpx2.Response(
                200,
                json={
                    "access_token": "access-new",
                    "refresh_token": "refresh-rotated",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                },
            )
        assert fields["code_verifier"] and fields["resource"]
        claims = {
            "iss": ISSUER,
            "aud": "oaiapp_fixture",
            "sub": "account-fixture",
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
            "nonce": nonce,
        }
        signed = JsonWebToken(["RS256"]).encode({"alg": "RS256", "kid": "fixture"}, claims, key)
        return httpx2.Response(
            200,
            json={
                "id_token": signed.decode(),
                "scope": SCOPE,
                "access_token": "access-old",
                "refresh_token": "refresh-original",
                "expires_in": 3600,
                "token_type": "Bearer",
            },
        )

    transport = httpx.MockTransport(respond)
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=transport, **kw))
    monkeypatch.setattr(
        "authlib.integrations.httpx_client.AsyncOAuth2Client",
        lambda **kw: AsyncOAuth2Client(transport=httpx2.MockTransport(respond), **kw),
    )
    try:
        start = await profiles.start("fixture")
        params = parse_qs(urlsplit(start["authorization_url"]).query)
        assert params["client_id"] == ["dynamic_agent_client"]
        assert params["code_challenge_method"] == ["S256"]
        assert params["ext_agent_host_id"] == [profiles.host_id]
        nonce = params["nonce"][0]
        callback = {"state": params["state"][0], "code": "code", "client_id": "oaiapp_fixture"}
        await profiles.callback(callback)
        with pytest.raises(AppError, match="already used"):
            await profiles.callback(callback)
        await profiles.configure({"id": "fixture", "provider": "chatgpt", "model": "account-model"})
        profiles.profiles["fixture"]["token"]["expires_at"] = 0
        tokens = await asyncio.gather(
            profiles.credential("fixture"), profiles.credential("fixture")
        )
        assert tokens == ["access-new", "access-new"]
        assert refreshes == [["refresh-original"]]
        path = profiles.directory / "fixture.json"
        assert path.stat().st_mode & 0o777 == 0o600
        assert json.loads(path.read_text())["token"]["refresh_token"] == "refresh-rotated"
        public = json.dumps(profiles.public()) + json.dumps(await service.snapshot())
        assert "access-new" not in public and "refresh-rotated" not in public
        returning = parse_qs(urlsplit((await profiles.start("fixture"))["authorization_url"]).query)
        assert returning["client_id"] == ["oaiapp_fixture"]
        assert (await profiles.logout("fixture"))["remote_revocation_confirmed"] is True
        with pytest.raises(AppError, match="changed during sign-in"):
            await profiles.callback({"state": returning["state"][0], "code": "late"})
        assert "token" not in json.loads(path.read_text())
        assert "fixture" not in service.drivers
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_refresh_failure_blocks_repeated_attempts(tmp_path, monkeypatch):
    service = ApplicationService(Workspace.resolve(str(tmp_path)))
    await service.open()
    profiles = ProviderProfiles(service, "http://127.0.0.1:8765")
    await profiles.initialize()
    calls = []

    def reject(request):
        calls.append(request)
        return httpx2.Response(400, json={"error": "invalid_grant"})

    monkeypatch.setattr(
        "authlib.integrations.httpx_client.AsyncOAuth2Client",
        lambda **kw: AsyncOAuth2Client(transport=httpx2.MockTransport(reject), **kw),
    )
    try:
        await profiles.save(
            {
                "id": "fixture",
                "provider": "chatgpt",
                "model": "model",
                "client_id": "oaiapp_fixture",
                "status": "ready",
                "token": {"access_token": "expired", "refresh_token": "revoked", "expires_at": 0},
            }
        )
        for _ in range(2):
            with pytest.raises(AppError) as error:
                await profiles.credential("fixture")
            assert error.value.code == "provider_authentication"
        assert len(calls) == 1
        assert profiles.public()[0]["status"] == "authentication_required"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_quota_blocks_the_profile_for_other_missions(tmp_path, monkeypatch):
    monkeypatch.setenv("ENTRYPLUG_FIXTURE_API_KEY", "fixture-only")
    service = ApplicationService(Workspace.resolve(str(tmp_path)))
    await service.open()
    profiles = ProviderProfiles(service, "http://127.0.0.1:8765")
    await profiles.initialize()
    calls = []

    async def quota(*args, **kwargs):
        calls.append(1)
        raise AppError("provider_quota", "Fixture quota", 409)

    monkeypatch.setattr("entryplug_agents.auth.OpenAIBackend.infer", quota)
    try:
        await profiles.configure(
            {
                "id": "fixture",
                "provider": "openai",
                "model": "fixture",
                "api_key_env": "ENTRYPLUG_FIXTURE_API_KEY",
            }
        )
        value = {
            "name": "Native",
            "instructions": "Read",
            "mode": "once",
            "agent": {"driver": "native", "decision_mode": "assisted", "profile": "fixture"},
        }
        mission = await service.command("mission.create", value, "native1")
        run = await service.command("mission.start", {"mission_id": mission["id"]}, "start1")
        async with asyncio.timeout(2):
            while (await service.store.get("runs", run["id"]))["health"] != "blocked":
                await asyncio.sleep(0.01)
        assert profiles.public()[0]["status"] == "quota_blocked"
        other = await service.command("mission.create", value, "native2")
        with pytest.raises(AppError):
            await service.command("mission.start", {"mission_id": other["id"]}, "start2")
        assert calls == [1]
    finally:
        await service.close()
