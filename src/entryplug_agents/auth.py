"""Backend-only profiles, official SIWC registration, and serialized renewal.

Files are protected by 0700/0600 permissions, not encrypted at rest.
No Codex installation identities or credentials are read.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import re
import secrets
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

from entryplug_agents.native import ModelReply, NativeDriver
from entryplug_agents.openai_backend import OpenAIBackend
from entryplug_app.contracts import AppError, text
from entryplug_app.workspace import private_write

ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
SCOPE = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"


def profile_id(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", value):
        raise AppError(
            "validation_error", "Profile ID must use letters, digits, hyphens or underscores"
        )
    return value


class ProviderProfiles:
    def __init__(self, service: Any, base_url: str):
        self.service, self.base_url = service, base_url
        self.directory: Path = service.workspace.config / "profiles"
        self.profiles: dict[str, dict[str, Any]] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.pending: dict[str, dict[str, Any]] = {}
        self.host_id = ""

    async def initialize(self) -> None:
        def load() -> tuple[str, list[dict[str, Any]]]:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = self.service.workspace.config / "host-id"
            if not path.exists():
                private_write(path, "urn:uuid:" + str(uuid.uuid4()))
            return path.read_text().strip(), [
                json.loads(p.read_text()) for p in self.directory.glob("*.json")
            ]

        self.host_id, loaded = await asyncio.to_thread(load)
        for profile in loaded:
            self.profiles[profile_id(profile["id"])] = profile
            self._install(profile)

    def _install(self, profile: dict[str, Any]) -> None:
        identifier = profile["id"]
        self.locks.setdefault(identifier, asyncio.Lock())
        if profile.get("model") and profile.get("status") == "ready":

            async def credential() -> str:
                return await self.credential(identifier)

            backend = OpenAIBackend(
                profile["model"], credential, chatgpt=profile["provider"] == "chatgpt"
            )
            profiles = self

            class ProfileBackend:
                async def infer(
                    self,
                    instructions: str,
                    history: list[dict[str, Any]],
                    tools: list[dict[str, Any]],
                    *,
                    on_text: Callable[[str], Awaitable[None]] | None = None,
                ) -> ModelReply:
                    try:
                        return await backend.infer(instructions, history, tools, on_text=on_text)
                    except AppError as error:
                        if error.code in {"provider_quota", "provider_authentication"}:
                            async with profiles.locks[identifier]:
                                current = profiles.profiles[identifier]
                                if current["status"] in {"ready", "model_required"}:
                                    await profiles.save(
                                        {
                                            **current,
                                            "status": "quota_blocked"
                                            if error.code == "provider_quota"
                                            else "authentication_required",
                                        }
                                    )
                                    await profiles.service.store.commit(
                                        [],
                                        "authentication.blocked",
                                        {"profile": identifier, "reason_code": error.code},
                                    )
                        raise

            self.service.drivers[identifier] = NativeDriver(ProfileBackend())
        else:
            self.service.drivers.pop(identifier, None)

    def public(self) -> list[dict[str, Any]]:
        return [
            {k: p.get(k) for k in ("id", "provider", "model", "status", "email", "subject")}
            | {
                "capabilities": OpenAIBackend.capabilities,
                "data_sent": ["instructions", "selected observations", "tool results"],
                "control": "managed",
                "live_validation": "not_run",
            }
            for p in self.profiles.values()
        ]

    async def save(self, profile: dict[str, Any]) -> None:
        await asyncio.to_thread(
            private_write,
            self.directory / (profile_id(profile["id"]) + ".json"),
            json.dumps(profile),
        )
        self.profiles[profile["id"]] = profile
        self._install(profile)

    async def configure(self, payload: dict[str, Any]) -> dict[str, Any]:
        identifier = profile_id(payload.get("id"))
        provider = payload.get("provider")
        if provider not in {"openai", "chatgpt"}:
            raise AppError(
                "provider_unsupported", "Only the tested OpenAI Responses routes are enabled"
            )
        self.locks.setdefault(identifier, asyncio.Lock())
        async with self.locks[identifier]:
            profile = dict(self.profiles.get(identifier, {"id": identifier, "provider": provider}))
            if profile["provider"] != provider:
                raise AppError(
                    "profile_conflict",
                    "Create a new profile for a different authentication route",
                    409,
                )
            profile["model"] = text(payload.get("model"), "model", 120)
            if provider == "openai":
                env = payload.get("api_key_env", "OPENAI_API_KEY")
                if not isinstance(env, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", env):
                    raise AppError(
                        "validation_error", "Expected an environment variable name, not a key"
                    )
                profile.update(
                    api_key_env=env,
                    status="ready" if os.environ.get(env) else "authentication_required",
                )
            else:
                profile["status"] = (
                    "ready"
                    if profile.get("token", {}).get("access_token")
                    else "authentication_required"
                )
            await self.save(profile)
        return next(p for p in self.public() if p["id"] == identifier)

    async def _discovery(self) -> dict[str, Any]:
        import httpx

        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            response = await client.get(ISSUER + "/.well-known/openid-configuration")
            response.raise_for_status()
            metadata = response.json()
        if metadata.get("issuer") != ISSUER:
            raise AppError("provider_identity_invalid", "OIDC issuer did not match", 409)
        for key in ("authorization_endpoint", "token_endpoint", "jwks_uri", "revocation_endpoint"):
            if key in metadata and not str(metadata[key]).startswith(ISSUER + "/"):
                raise AppError("provider_identity_invalid", "Unexpected OpenAI OIDC endpoint", 409)
        return cast(dict[str, Any], metadata)

    async def start(self, identifier: str) -> dict[str, Any]:
        oauth_client = importlib.import_module(
            "authlib.integrations.httpx_client"
        ).AsyncOAuth2Client
        identifier = profile_id(identifier)
        if not self.host_id:
            await self.initialize()
        profile = self.profiles.get(identifier, {"id": identifier, "provider": "chatgpt"})
        if profile["provider"] != "chatgpt":
            raise AppError("profile_conflict", "Use a ChatGPT profile for sign-in", 409)
        now = time.monotonic()
        self.pending = {k: v for k, v in self.pending.items() if v["expires"] > now}
        if len(self.pending) >= 8:
            raise AppError("login_limit", "Too many pending sign-ins", 429)
        metadata = await self._discovery()
        client_id = profile.get("client_id", "dynamic_agent_client")
        nonce, verifier, state = (
            secrets.token_urlsafe(40),
            secrets.token_urlsafe(64),
            secrets.token_urlsafe(40),
        )
        redirect = self.base_url + "/auth/callback"
        async with oauth_client(
            client_id=client_id,
            scope=SCOPE,
            redirect_uri=redirect,
            code_challenge_method="S256",
            token_endpoint_auth_method="none",
        ) as client:
            extra = {"ext_agent_host_id": self.host_id, "nonce": nonce, "resource": RESOURCE}
            if client_id == "dynamic_agent_client":
                extra["agent_name_hint"] = "Entryplug"
            url, _ = client.create_authorization_url(
                metadata["authorization_endpoint"], state=state, code_verifier=verifier, **extra
            )
        self.pending[state] = {
            "profile": identifier,
            "client_id": client_id,
            "nonce": nonce,
            "verifier": verifier,
            "redirect": redirect,
            "metadata": metadata,
            "expires": now + 600,
            "generation": profile.get("generation", 0),
        }
        return {"authorization_url": url, "profile": identifier}

    async def callback(self, params: dict[str, str]) -> None:
        import httpx

        oauth_client = importlib.import_module(
            "authlib.integrations.httpx_client"
        ).AsyncOAuth2Client
        jwt = importlib.import_module("authlib.jose").JsonWebToken

        transaction = self.pending.pop(params.get("state", ""), None)
        if not transaction or transaction["expires"] < time.monotonic():
            raise AppError(
                "login_expired", "Sign-in state is missing, expired, or already used", 401
            )
        if "error" in params:
            raise AppError("login_declined", "ChatGPT sign-in was not authorized", 401)
        identifier = transaction["profile"]
        client_id = transaction["client_id"]
        if client_id == "dynamic_agent_client":
            client_id = params.get("client_id", "")
            if not client_id.startswith("oaiapp_"):
                raise AppError(
                    "registration_incomplete", "OpenAI did not issue an application client ID", 401
                )
        elif params.get("client_id", client_id) != client_id:
            raise AppError("registration_mismatch", "Client registration changed unexpectedly", 401)
        self.locks.setdefault(identifier, asyncio.Lock())
        async with self.locks[identifier]:
            prior = self.profiles.get(identifier, {})
            if prior.get("generation", 0) != transaction["generation"]:
                raise AppError("login_superseded", "Profile changed during sign-in", 409)
            async with oauth_client(
                client_id=client_id, token_endpoint_auth_method="none", timeout=15
            ) as client:
                token = await client.fetch_token(
                    transaction["metadata"]["token_endpoint"],
                    grant_type="authorization_code",
                    code=text(params.get("code"), "authorization code", 4096),
                    code_verifier=transaction["verifier"],
                    redirect_uri=transaction["redirect"],
                    resource=RESOURCE,
                )
            async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
                response = await client.get(transaction["metadata"]["jwks_uri"])
                response.raise_for_status()
                keys = response.json()
            try:
                claims = jwt(["RS256", "ES256"]).decode(
                    token["id_token"],
                    keys,
                    claims_options={
                        "iss": {"essential": True, "value": ISSUER},
                        "aud": {"essential": True, "value": client_id},
                        "exp": {"essential": True},
                        "iat": {"essential": True},
                        "sub": {"essential": True},
                        "nonce": {"essential": True, "value": transaction["nonce"]},
                    },
                )
                claims.validate(leeway=5)
                if not claims["sub"] or (
                    prior.get("subject") and prior["subject"] != claims["sub"]
                ):
                    raise ValueError("identity mismatch")
                if "chatgpt.tokens.use.direct" not in token.get("scope", "").split():
                    raise ValueError("plan permission absent")
            except Exception as error:
                raise AppError(
                    "provider_identity_invalid",
                    "Identity or ChatGPT plan permission could not be validated",
                    401,
                ) from error
            await self.save(
                {
                    **prior,
                    "id": identifier,
                    "provider": "chatgpt",
                    "client_id": client_id,
                    "subject": claims["sub"],
                    "email": claims.get("email"),
                    "token": dict(token),
                    "status": "ready" if prior.get("model") else "model_required",
                    "generation": prior.get("generation", 0) + 1,
                }
            )

    async def credential(self, identifier: str) -> str:
        async with self.locks[identifier]:
            profile = self.profiles[identifier]
            if profile["status"] not in {"ready", "model_required"}:
                raise AppError("provider_authentication", "Reconnect this provider profile", 409)
            if profile["provider"] == "openai":
                api_key = os.environ.get(profile["api_key_env"])
                if not api_key:
                    raise AppError(
                        "provider_authentication",
                        "Configured API key environment variable is absent",
                        409,
                    )
                return api_key
            token = profile.get("token", {})
            if token.get("expires_at", 0) <= time.time() + 60:
                oauth_client = importlib.import_module(
                    "authlib.integrations.httpx_client"
                ).AsyncOAuth2Client
                try:
                    async with oauth_client(
                        client_id=profile["client_id"],
                        token_endpoint_auth_method="none",
                        timeout=15,
                    ) as client:
                        refreshed = await client.refresh_token(
                            ISSUER + "/api/accounts/oauth/token",
                            refresh_token=token["refresh_token"],
                            resource=RESOURCE,
                        )
                    token = {**token, **refreshed}
                    await self.save({**profile, "token": token})
                except Exception as error:
                    await self.save({**profile, "status": "authentication_required"})
                    raise AppError(
                        "provider_authentication",
                        "Token renewal failed; reconnect this account",
                        409,
                    ) from error
            return str(token["access_token"])

    async def models(self, identifier: str) -> list[dict[str, str]]:
        import httpx

        token = await self.credential(profile_id(identifier))
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            response = await client.get(
                RESOURCE + "/models", headers={"Authorization": "Bearer " + token}
            )
            response.raise_for_status()
            result = response.json()
        if self.profiles[identifier]["provider"] == "chatgpt":
            return [
                {"id": m["slug"], "name": m["display_name"]}
                for m in result["models"]
                if m.get("visibility") == "list"
            ]
        return [{"id": m["id"], "name": m["id"]} for m in result["data"]]

    async def logout(self, identifier: str) -> dict[str, Any]:
        import httpx

        identifier = profile_id(identifier)
        if identifier not in self.profiles:
            raise AppError("not_found", "Profile not found", 404)
        async with self.locks[identifier]:
            profile = self.profiles[identifier]
            confirmed = False
            try:
                if profile["provider"] == "chatgpt" and profile.get("token", {}).get(
                    "refresh_token"
                ):
                    metadata = await self._discovery()
                    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
                        response = await client.post(
                            metadata["revocation_endpoint"],
                            data={
                                "token": profile["token"]["refresh_token"],
                                "token_type_hint": "refresh_token",
                                "client_id": profile["client_id"],
                            },
                        )
                        confirmed = response.status_code == 200
            except Exception:
                confirmed = False
            await self.save(
                {k: v for k, v in profile.items() if k != "token"}
                | {"status": "signed_out", "generation": profile.get("generation", 0) + 1}
            )
            return {"status": "signed_out", "remote_revocation_confirmed": confirmed}
