"""Single-worker local HTTP/SSE adapter; no mission policy in routes."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import re
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from importlib.resources import files
from typing import Any

from sse_starlette.sse import EventSourceResponse
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from entryplug_app.contracts import (
    API_VERSION,
    BUILD_ID,
    MAX_REQUEST_BYTES,
    AppError,
    canonical,
    digest,
    text,
)
from entryplug_app.service import ApplicationService
from entryplug_server.schemas import COMMANDS, openapi
from entryplug_server.security import LocalSecurity


async def read_json(request: Request) -> dict[str, Any]:
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > MAX_REQUEST_BYTES:
            raise AppError("request_too_large", "Request exceeds 128 KiB", 413)
    try:
        value = json.loads(data or b"{}")
        canonical(value)
    except (ValueError, UnicodeError) as error:
        raise AppError("validation_error", "Request must be valid finite JSON") from error
    if not isinstance(value, dict):
        raise AppError("validation_error", "Request body must be an object")
    return value


def create_app(
    service: ApplicationService,
    *,
    base_url: str = "http://127.0.0.1:8765",
    security: LocalSecurity | None = None,
    manage_lifespan: bool = True,
    close_service: bool = False,
) -> Starlette:
    # CLI prepares the private directories before reading or creating credentials.
    service.workspace.prepare()
    security = security or LocalSecurity(service.workspace.credential(), base_url)

    from entryplug_agents.auth import ProviderProfiles
    from entryplug_app.connections import Connections

    providers = ProviderProfiles(service, base_url)
    connections = Connections(service)
    from entryplug_server.protocols import ProtocolGateway

    gateways = {kind: ProtocolGateway(kind, service, security, base_url) for kind in ("mcp", "a2a")}

    admin_lock = asyncio.Lock()

    async def administrative(
        request: Request,
        action: str,
        payload: dict[str, Any],
        perform: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        key = text(request.headers.get("idempotency-key"), "Idempotency-Key", 200)
        fingerprint = digest({"action": action, "payload": payload})
        async with admin_lock:
            prior = await service.store.request("owner", key, fingerprint)
            if prior is not None:
                return prior
            identifier = digest(key)
            try:
                await service.store.get("admin_intents", identifier)
            except AppError as error:
                if error.code != "not_found":
                    raise
            else:
                raise AppError(
                    "command_unconfirmed",
                    "Previous request was interrupted; inspect current state "
                    "before retrying with a new key",
                    409,
                )
            await service.store.check_capacity()
            await service.store.commit(
                [("admin_intents", identifier, {"id": identifier, "action": action})],
                "request.recorded",
                {"action": action},
            )
            result = await perform()
            await service.store.commit(
                [],
                action,
                {"action": action, "profile": payload.get("profile", payload.get("id"))},
                request=("owner", key, fingerprint, result),
            )
            return result

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        if manage_lifespan:
            await service.open()
        try:
            await providers.initialize()
            await connections.restore()
            yield
        finally:
            for gateway in gateways.values():
                await gateway.close()
            if manage_lifespan or close_service:
                if close_service:
                    (service.workspace.state / "service.json").unlink(missing_ok=True)
                await service.close()

    async def api(request: Request) -> Response:
        path = request.path_params.get("path", "")
        if path == "session/exchange" and request.method == "POST":
            payload = await read_json(request)
            token = payload.get("token")
            if not isinstance(token, str):
                raise AppError("validation_error", "Expected a one-use bootstrap token")
            session, csrf = security.exchange(token)
            response = JSONResponse(
                {"csrf": csrf, "api_version": API_VERSION, "build_id": BUILD_ID}
            )
            response.set_cookie(
                "entryplug_session",
                session,
                httponly=True,
                samesite="strict",
                secure=base_url.startswith("https:"),
                max_age=43200,
            )
            return response
        actor = await security.authenticate(request, service)
        external = actor != "owner"
        if (
            external
            and path not in {"health", "bodies", "capabilities", "operations"}
            and not path.startswith(("operations/", "evidence/"))
        ):
            raise AppError("forbidden", "This route is outside the external attachment scope", 403)
        if path == "session" and request.method == "GET":
            return JSONResponse(
                {"csrf": security.csrf(request), "build_id": BUILD_ID, "api_version": API_VERSION}
            )
        if path == "session/bootstrap" and request.method == "POST":
            return JSONResponse({"bootstrap": security.bootstrap()})
        if path == "providers" and request.method == "GET":
            installed = all(
                importlib.util.find_spec(name) is not None for name in ("openai", "authlib")
            )
            return JSONResponse(
                {
                    "openai_installed": installed,
                    "install_command": (
                        "python -m pip install './dist/entryplug-0.0.1-py3-none-any.whl[ui,openai]'"
                    ),
                    "restart_required": not installed,
                }
            )
        if path == "simulations" and request.method == "GET":
            return JSONResponse(service.simulations.snapshot())
        if path == "simulations" and request.method == "POST":
            payload = await read_json(request)
            return JSONResponse(
                await administrative(
                    request,
                    "simulation.created",
                    payload,
                    lambda: service.simulations.create(payload),
                )
            )
        if (
            path.startswith("simulations/")
            and path.endswith("/control")
            and request.method == "POST"
        ):
            identifier = path.split("/")[1]
            payload = await read_json(request)
            return JSONResponse(
                await administrative(
                    request,
                    "simulation.control",
                    {**payload, "id": identifier},
                    lambda: service.simulations.control(identifier, payload),
                )
            )
        if path == "profiles" and request.method == "GET":
            return JSONResponse(providers.public())
        if path == "profiles" and request.method == "POST":
            payload = await read_json(request)
            return JSONResponse(
                await administrative(
                    request, "profile.configured", payload, lambda: providers.configure(payload)
                ),
                status_code=202,
            )
        if path == "auth/chatgpt/start" and request.method == "POST":
            payload = await read_json(request)
            return JSONResponse(
                await administrative(
                    request,
                    "authentication.started",
                    payload,
                    lambda: providers.start(payload.get("profile", "personal-chatgpt")),
                )
            )
        if path == "auth/logout" and request.method == "POST":
            payload = await read_json(request)
            return JSONResponse(
                await administrative(
                    request,
                    "authentication.signed_out",
                    payload,
                    lambda: providers.logout(payload.get("profile", "")),
                )
            )
        if path.startswith("profiles/") and path.endswith("/models") and request.method == "GET":
            return JSONResponse(await providers.models(path.split("/")[1]))
        if path == "connections" and request.method == "POST":
            payload = await read_json(request)
            return JSONResponse(
                await administrative(
                    request, "connection.created", payload, lambda: connections.create(payload)
                ),
                status_code=202,
            )
        if path.startswith("connections/") and path.endswith("/test") and request.method == "POST":
            identifier = path.split("/")[1]
            return JSONResponse(await connections.probe(identifier))
        if path.startswith("connections/") and path.endswith("/retry") and request.method == "POST":
            identifier = path.split("/")[1]
            return JSONResponse(
                await administrative(
                    request,
                    "connection.retrying",
                    {"id": identifier},
                    lambda: connections.retry(identifier),
                ),
                status_code=202,
            )
        if path.startswith("connections/") and request.method == "DELETE":
            identifier = path.split("/")[1]
            return JSONResponse(
                await administrative(
                    request,
                    "connection.disconnected",
                    {"id": identifier},
                    lambda: connections.disconnect(identifier),
                ),
                status_code=202,
            )
        if request.method == "GET":
            if path == "health":
                return JSONResponse(
                    {
                        "status": "ok",
                        "api_version": API_VERSION,
                        "build_id": BUILD_ID,
                        "workspace_id": service.store.workspace_id,
                        "service_instance_id": service.store.instance_id,
                    }
                )
            if path == "snapshot":
                return JSONResponse(await service.snapshot())
            if path == "openapi.json":
                return JSONResponse(openapi())
            if path in {"bodies", "capabilities"}:
                bodies = await service.bodies()
                if external:
                    attachment = await service.store.get("attachments", actor)
                    bodies = [b for b in bodies if b["id"] == attachment["body_id"]]
                    for body in bodies:
                        body["capabilities"] = [
                            c for c in body["capabilities"] if c["name"] in attachment["allow"]
                        ]
                return JSONResponse(
                    bodies if path == "bodies" else [c for b in bodies for c in b["capabilities"]]
                )
            if path == "topology":
                return JSONResponse(await service.topology())
            if path == "agents":
                return JSONResponse(
                    [
                        {
                            "id": name,
                            "driver": name
                            if name in {"rules", "inspection", "simulated", "scripted"}
                            else "native",
                            "status": "ready",
                            "control": "managed",
                        }
                        for name in service.drivers
                    ]
                )
            if path in {"missions", "alerts", "connections"}:
                return JSONResponse(await service.store.list(path))
            if path == "events":
                return await events(request)
            parts = path.split("/")
            if len(parts) in {2, 3} and parts[0] in {"missions", "runs", "operations", "evidence"}:
                document = await service.store.get(parts[0], parts[1])
                if external:
                    op = (
                        document
                        if parts[0] == "operations"
                        else await service.store.get("operations", document.get("operation_id", ""))
                    )
                    await service.authorize_operation(op, actor)
                if len(parts) == 3:
                    if parts[0] != "evidence" or parts[2] != "content":
                        raise AppError("not_found", "Route not found", 404)
                    return JSONResponse(document["content"])
                return JSONResponse(
                    document,
                    headers={"ETag": f'"{document["revision"]}"'} if "revision" in document else {},
                )
        if path == "missions/validate" and request.method == "POST":
            return JSONResponse(await service.validate(await read_json(request)))
        if path == "demo/step" and request.method == "POST":
            payload = await read_json(request)
            return JSONResponse(
                await administrative(
                    request,
                    "demo.stepped",
                    payload,
                    lambda: service.demo_step(payload.get("action", "")),
                )
            )
        for (method, template), action in COMMANDS.items():
            if request.method != method:
                continue
            pattern = re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", template)
            match = re.fullmatch(pattern, path)
            if not match:
                continue
            payload = await read_json(request)
            # Path identifiers are authoritative; clients cannot replace them in JSON.
            payload.update(match.groupdict())
            revision_header = request.headers.get("if-match")
            revision: int | None = None
            if revision_header is not None:
                try:
                    revision = int(revision_header.strip('"'))
                except ValueError as error:
                    raise AppError(
                        "validation_error", "If-Match must be a revision number"
                    ) from error
            result = await service.command(
                action,
                payload,
                request.headers.get("idempotency-key", ""),
                actor=actor,
                revision=revision,
            )
            return JSONResponse(result, status_code=202)
        raise AppError("not_found", "Route not found", 404)

    async def events(request: Request) -> Response:
        raw = request.headers.get("last-event-id") or request.query_params.get("after", "0")
        if ":" in raw:
            workspace_id, raw = raw.rsplit(":", 1)
            if workspace_id != service.store.workspace_id:
                raise AppError("invalid_cursor", "Cursor belongs to another workspace")
        try:
            after = int(raw)
        except ValueError as error:
            raise AppError(
                "invalid_cursor", "Cursor must be a persistent sequence number"
            ) from error
        run_id = request.query_params.get("run_id")
        first = await service.store.events(after)

        async def stream() -> AsyncIterator[dict[str, str]]:
            cursor, batch = after, first
            queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=64)
            service.subscribers.add(queue)
            try:
                while not service.closed:
                    for event in batch:
                        cursor = event["seq"]
                        if run_id and event.get("run_id") != run_id:
                            continue
                        yield {
                            "id": f"{service.store.workspace_id}:{cursor}",
                            "event": "entryplug",
                            "data": canonical(event),
                        }
                    while not queue.empty():
                        event = queue.get_nowait()
                        if event["type"] == "resync":
                            yield {"event": "resync", "data": canonical(event)}
                            return
                        if not run_id or event.get("run_id") == run_id:
                            yield {"event": "activity", "data": canonical(event)}
                    await asyncio.sleep(0.05)
                    try:
                        await security.authenticate(request, service)
                        batch = await service.store.events(cursor)
                    except AppError as error:
                        yield {"event": "resync", "data": canonical({"code": error.code})}
                        return
            finally:
                service.subscribers.discard(queue)

        return EventSourceResponse(
            stream(), ping=15, send_timeout=5, headers={"Cache-Control": "no-store"}
        )

    static = files("entryplug_ui").joinpath("static")

    async def index(request: Request) -> Response:
        path = str(static.joinpath("index.html"))
        return FileResponse(path, headers={"Cache-Control": "no-store"})

    async def recording(request: Request) -> Response:
        return FileResponse(str(static.joinpath("assets/city-walk.webm")), media_type="video/webm")

    async def oauth_callback(request: Request) -> Response:
        try:
            await providers.callback(dict(request.query_params))
        except AppError:
            raise
        except Exception as error:
            raise AppError(
                "login_failed", "Provider sign-in failed; no existing profile was replaced", 401
            ) from error
        return RedirectResponse("/#connections", status_code=303)

    app = Starlette(
        lifespan=lifespan,
        routes=[
            Route("/auth/callback", oauth_callback),
            Route("/assets/city-walk.webm", recording),
            Mount("/mcp", gateways["mcp"]),
            Mount("/a2a", gateways["a2a"]),
            Route("/v1/{path:path}", api, methods=["GET", "POST", "PATCH", "DELETE"]),
            Route("/", index),
            Mount(
                "/assets",
                GZipMiddleware(
                    StaticFiles(directory=str(static.joinpath("assets")), check_dir=False),
                    minimum_size=1000,
                ),
            ),
        ],
    )

    async def local_boundary(request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = secrets.token_hex(12)
        try:
            # The provider's top-level OAuth redirect is authenticated by one-use state.
            if request.url.path == "/auth/callback":
                if request.headers.get("host") != security.host:
                    raise AppError("invalid_host", "Invalid callback authority", 403)
            else:
                security.check_origin(request)
            response = await call_next(request)
        except AppError as error:
            response = JSONResponse(
                {
                    "code": error.code,
                    "message": str(error),
                    "retryable": error.retryable,
                    "request_id": request_id,
                },
                status_code=error.status,
            )
        except (KeyError, TypeError, ValueError):
            response = JSONResponse(
                {
                    "code": "validation_error",
                    "message": "Invalid request fields",
                    "retryable": False,
                    "request_id": request_id,
                },
                status_code=400,
            )
        except ImportError:
            response = JSONResponse(
                {
                    "code": "extra_required",
                    "message": "Install the optional provider or protocol extra for this feature",
                    "retryable": False,
                    "request_id": request_id,
                },
                status_code=503,
            )
        except Exception:
            response = JSONResponse(
                {
                    "code": "service_error",
                    "message": "Request did not complete; inspect current state before retrying",
                    "retryable": False,
                    "request_id": request_id,
                },
                status_code=500,
            )
        response.headers.update(
            {
                "X-Request-ID": request_id,
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": (
                    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                    "img-src 'self' data:; connect-src 'self'; "
                    "frame-ancestors 'none'; base-uri 'none'"
                ),
                "Cache-Control": "no-store",
                "X-Entryplug-Build": BUILD_ID,
            }
        )
        return response

    app.add_middleware(BaseHTTPMiddleware, dispatch=local_boundary)
    app.state.service = service
    app.state.security = security
    app.state.providers = providers
    app.state.connections = connections
    app.state.gateways = gateways
    return app
