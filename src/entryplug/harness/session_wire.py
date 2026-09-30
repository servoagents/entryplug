"""Bounded JSON requests for a local, fixture-owned Session connection."""

from __future__ import annotations

import math
from collections.abc import Mapping

from entryplug.core.evidence import JsonValue, json_object
from entryplug.harness.agent import public_view
from entryplug.harness.session import Session


def _text(request: Mapping[str, object], key: str) -> str:
    value = request.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a nonempty string")
    return value


async def dispatch_session_request(
    session: Session, request: Mapping[str, object]
) -> dict[str, JsonValue]:
    """Allow only public Session verbs; never expose fixture or provider handles."""

    kind = _text(request, "kind")
    if kind == "observe":
        return public_view(await session.observe())
    if kind == "act":
        arguments = request.get("arguments")
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be an object")
        operation = await session.act(
            _text(request, "capability"),
            arguments,
            request_id=_text(request, "request_id"),
            expected_runtime_id=_text(request, "runtime_id"),
        )
        return json_object(await session.inspect(operation.operation_id), "operation")
    if kind in {"wait", "cancel"}:
        operation_id = _text(request, "operation_id")
        if kind == "wait":
            timeout = request.get("timeout_seconds")
            if (
                isinstance(timeout, bool)
                or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout)
                or not 0 <= timeout <= 5
            ):
                raise ValueError("timeout_seconds must be finite and within 0..5")
            operation = await session.wait(operation_id, float(timeout))
        else:
            operation = await session.cancel(operation_id)
        return json_object(
            await session.inspect(operation.operation_id, detail="result"), "operation"
        )
    if kind == "inspect":
        detail = request.get("detail", "summary")
        if not isinstance(detail, str) or detail not in {"summary", "result"}:
            raise ValueError("detail must be summary or result")
        return json_object(
            await session.inspect(_text(request, "reference"), detail=detail),
            "inspection",
        )
    raise ValueError("unknown Session request")
