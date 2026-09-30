"""Local client for the scenario's bounded Session socket."""

from __future__ import annotations

import json
import socket
import time
from pathlib import Path
from typing import Any


class SessionConnectionError(RuntimeError):
    pass


class HybridSessionClient:
    def __init__(self, socket_path: Path) -> None:
        self.connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.connection.settimeout(15.0)
        self.connection.connect(str(socket_path))
        self.stream = self.connection.makefile("rwb")

    def request(self, kind: str, **arguments: object) -> dict[str, Any]:
        message = json.dumps({"kind": kind, **arguments}, allow_nan=False).encode()
        if len(message) > 16_384:
            raise SessionConnectionError("Session request exceeds the bounded message size")
        self.stream.write(message + b"\n")
        self.stream.flush()
        line = self.stream.readline()
        if not line:
            raise SessionConnectionError("Session closed without a reply")
        reply = json.loads(line)
        if not isinstance(reply, dict) or not reply.get("ok"):
            reason = reply.get("error", "malformed reply") if isinstance(reply, dict) else "reply"
            raise SessionConnectionError(f"Session refused {kind}: {reason}")
        value = reply.get("value")
        if not isinstance(value, dict):
            raise SessionConnectionError("Session returned a malformed value")
        return value

    def close(self) -> None:
        try:
            self.request("close")
        finally:
            self.stream.close()
            self.connection.close()


def run_scripted(socket_path: Path, run_id: str) -> dict[str, object]:
    """Exercise the remote agent boundary without exporting observations."""

    client = HybridSessionClient(socket_path)
    operations: list[dict[str, Any]] = []
    try:
        view = client.request("observe")
        if view.get("runtime_id") != run_id:
            raise SessionConnectionError("Session runtime identity changed")
        capabilities = view.get("capabilities")
        if not isinstance(capabilities, list) or not any(
            isinstance(item, dict) and item.get("name") == "inspect_target"
            for item in capabilities
        ):
            raise SessionConnectionError("inspect_target was not advertised")
        for label in ("first", "warm"):
            admitted = client.request(
                "act",
                capability="inspect_target",
                arguments={"target_id": "bench-marker"},
                request_id=f"{run_id}-{label}",
                runtime_id=run_id,
            )
            operation_id = admitted.get("operation_id")
            if not isinstance(operation_id, str):
                raise SessionConnectionError("Session did not return an operation ID")
            deadline = time.monotonic() + 45.0
            while admitted.get("lifecycle") not in {
                "succeeded", "failed", "canceled", "rejected", "indeterminate"
            }:
                if time.monotonic() >= deadline:
                    raise SessionConnectionError("inspection exceeded the client wait budget")
                admitted = client.request(
                    "wait", operation_id=operation_id, timeout_seconds=5.0
                )
            result = client.request("inspect", reference=operation_id, detail="result")
            operations.append(result)
            if result.get("lifecycle") != "succeeded":
                break
        return {"operations": operations, "runtime_id": run_id}
    finally:
        client.close()
