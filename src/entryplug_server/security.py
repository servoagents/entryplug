"""Loopback credentials, one-use browser bootstrap and same-origin requests."""

from __future__ import annotations

import secrets
import time
from typing import Any
from urllib.parse import urlsplit

from entryplug_app.contracts import AppError, digest


class LocalSecurity:
    def __init__(self, credential: str, base_url: str):
        self.credential = credential
        self.origin = base_url.rstrip("/")
        parsed = urlsplit(self.origin)
        if parsed.hostname != "127.0.0.1" or parsed.scheme not in {"http", "https"}:
            raise ValueError("The local service requires a 127.0.0.1 origin")
        self.host = parsed.netloc
        self.bootstraps: dict[str, float] = {}
        self.sessions: dict[str, tuple[float, str]] = {}

    def bootstrap(self) -> str:
        now = time.monotonic()
        self.bootstraps = {k: v for k, v in self.bootstraps.items() if v > now}
        if len(self.bootstraps) >= 32:
            raise AppError("bootstrap_limit", "Too many outstanding browser sign-ins", 429)
        token = secrets.token_urlsafe(32)
        self.bootstraps[digest(token)] = now + 60
        return token

    def exchange(self, token: str) -> tuple[str, str]:
        expires = self.bootstraps.pop(digest(token), 0)
        if expires <= time.monotonic():
            raise AppError("bootstrap_expired", "Open the console again with entryplug ui", 401)
        now = time.monotonic()
        self.sessions = {k: v for k, v in self.sessions.items() if v[0] > now}
        session, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.sessions[digest(session)] = (now + 12 * 3600, csrf)
        return session, csrf

    def check_origin(self, request: Any) -> None:
        if request.headers.get("host") != self.host:
            raise AppError("invalid_host", "Host is not this service's loopback authority", 403)
        origin = request.headers.get("origin")
        if origin is not None and origin != self.origin:
            raise AppError("invalid_origin", "Origin is not this service's console", 403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            raise AppError("invalid_origin", "Cross-site requests are refused", 403)

    async def authenticate(self, request: Any, service: Any) -> str:
        bearer = request.headers.get("authorization", "")
        if bearer.startswith("Bearer "):
            token = bearer[7:]
            if secrets.compare_digest(token, self.credential):
                return "owner"
            for attachment in await service.store.list("attachments"):
                if attachment["status"] == "active" and secrets.compare_digest(
                    attachment["token_hash"], digest(token)
                ):
                    return str(attachment["id"])
        cookie = request.cookies.get("entryplug_session", "")
        session = self.sessions.get(digest(cookie))
        if session and session[0] > time.monotonic():
            if request.method not in {"GET", "HEAD"}:
                if request.headers.get("origin") != self.origin or not secrets.compare_digest(
                    request.headers.get("x-entryplug-csrf", ""), session[1]
                ):
                    raise AppError(
                        "csrf_required",
                        "A same-origin console request and CSRF token are required",
                        403,
                    )
            return "owner"
        raise AppError("authentication_required", "A local service credential is required", 401)

    def csrf(self, request: Any) -> str | None:
        session = self.sessions.get(digest(request.cookies.get("entryplug_session", "")))
        return session[1] if session and session[0] > time.monotonic() else None
