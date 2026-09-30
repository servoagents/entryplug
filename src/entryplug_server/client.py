"""Async HTTP/SSE client; never owns Session or mission policy."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any, cast

import httpx

from entryplug_app.contracts import AppError
from entryplug_app.workspace import Workspace


class EntryplugClient:
    def __init__(
        self, base_url: str, token: str, *, transport: httpx.AsyncBaseTransport | None = None
    ):
        self.http = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": "Bearer " + token},
            timeout=15,
            transport=transport,
            trust_env=False,
        )

    @classmethod
    def from_workspace(cls, name: str = "default") -> EntryplugClient:
        workspace = Workspace.resolve(name)
        try:
            location = json.loads((workspace.state / "service.json").read_text())
            credential = (workspace.config / "client.token").read_text().strip()
        except (OSError, ValueError) as error:
            raise AppError(
                "service_not_running", "Start entryplug serve --workspace " + name, 503
            ) from error
        return cls(location["base_url"], credential)

    @staticmethod
    def _check(response: httpx.Response) -> Any:
        if response.is_error:
            try:
                error = response.json()
            except ValueError:
                error = {}
            raise AppError(
                error.get("code", "http_error"),
                error.get("message", "Service request failed"),
                response.status_code,
                retryable=error.get("retryable", False),
            )
        return response.json()

    async def request(
        self,
        method: str,
        path: str,
        data: Any = None,
        *,
        key: str | None = None,
        revision: int | None = None,
    ) -> Any:
        headers = {"Idempotency-Key": key or uuid.uuid4().hex}
        if revision is not None:
            headers["If-Match"] = str(revision)
        try:
            response = await self.http.request(
                method, "/v1/" + path.lstrip("/"), json=data, headers=headers
            )
        except httpx.RequestError as error:
            raise AppError(
                "service_not_running",
                "Service is unreachable; start entryplug serve",
                503,
                retryable=True,
            ) from error
        return self._check(response)

    async def create(self, definition: dict[str, Any], *, key: str | None = None) -> dict[str, Any]:
        return cast(dict[str, Any], await self.request("POST", "missions", definition, key=key))

    async def start(self, mission_id: str, *, key: str | None = None) -> dict[str, Any]:
        return cast(
            dict[str, Any], await self.request("POST", f"missions/{mission_id}/runs", {}, key=key)
        )

    async def snapshot(self) -> dict[str, Any]:
        return cast(dict[str, Any], await self.request("GET", "snapshot"))

    async def events(
        self, *, after: int = 0, run_id: str | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        params = {"after": str(after)}
        if run_id:
            params["run_id"] = run_id
        try:
            async with self.http.stream(
                "GET", "/v1/events", params=params, timeout=None
            ) as response:
                if response.is_error:
                    await response.aread()
                    self._check(response)
                event_type = ""
                async for line in response.aiter_lines():
                    if line.startswith("event: "):
                        event_type = line[7:]
                    if line.startswith("data: "):
                        event = json.loads(line[6:])
                        if event_type == "resync":
                            raise AppError(
                                event["code"], "Refresh snapshot before reconnecting", 410
                            )
                        yield event
        except httpx.RequestError as error:
            raise AppError(
                "service_not_running", "Event observer disconnected", 503, retryable=True
            ) from error

    async def __aenter__(self) -> EntryplugClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.http.aclose()
