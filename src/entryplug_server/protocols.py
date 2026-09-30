"""Lazy resident MCP/A2A mounts over the existing exports and one scoped Session."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from entryplug_app.borrowed_session import BorrowedSession
from entryplug_app.contracts import AppError


class ProtocolGateway:
    def __init__(self, kind: str, service: Any, security: Any, base_url: str):
        self.kind, self.service, self.security, self.base_url = kind, service, security, base_url
        self.apps: dict[str, Any] = {}
        self.tasks: dict[str, asyncio.Task[Any]] = {}
        self.lock = asyncio.Lock()
        self.shutdown = asyncio.Event()

    async def _application(self, attachment_id: str) -> Any:
        async with self.lock:
            if attachment_id in self.apps:
                return self.apps[attachment_id]
            session = BorrowedSession(self.service, attachment_id)
            if self.kind == "mcp":
                from entryplug_mcp.server import create_mcp_server

                @asynccontextmanager
                async def lifespan(server: Any) -> AsyncIterator[BorrowedSession]:
                    yield session

                app = create_mcp_server(lifespan).streamable_http_app(
                    streamable_http_path="/",
                    json_response=True,
                    stateless_http=True,
                    max_request_body_size=131072,
                )
            else:
                from entryplug_a2a.server import create_a2a_server

                server = await create_a2a_server(
                    session, base_url=f"{self.base_url}/a2a/{attachment_id}"
                )
                app = server.app
            ready = asyncio.Event()

            async def owner() -> None:
                try:
                    async with app.router.lifespan_context(app):
                        ready.set()
                        await self.shutdown.wait()
                finally:
                    ready.set()

            task = asyncio.create_task(owner())
            self.tasks[attachment_id] = task
            await ready.wait()
            if task.done():
                await task
                raise AppError("protocol_unavailable", "Protocol server did not start", 503)
            self.apps[attachment_id] = app
            return app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        request = Request(scope, receive)
        try:
            actor = await self.security.authenticate(request, self.service)
            relative = scope["path"][len(scope.get("root_path", "")) :].lstrip("/")
            identifier, _, remaining = relative.partition("/")
            attachment = await self.service.store.get("attachments", identifier)
            if attachment["status"] != "active" or actor not in {"owner", identifier}:
                raise AppError("forbidden", "Protocol access is outside this attachment", 403)
            app = await self._application(identifier)
            forwarded = {
                **scope,
                "path": "/" + remaining,
                "raw_path": ("/" + remaining).encode(),
                "root_path": "",
            }
            await app(forwarded, receive, send)
        except ImportError:
            await JSONResponse(
                {"code": "protocol_unavailable", "message": f"Install entryplug[{self.kind}]"},
                status_code=503,
            )(scope, receive, send)
        except AppError as error:
            await JSONResponse(
                {"code": error.code, "message": str(error)}, status_code=error.status
            )(scope, receive, send)

    async def close(self) -> None:
        self.shutdown.set()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
