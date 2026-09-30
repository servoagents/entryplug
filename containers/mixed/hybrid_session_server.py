"""Scenario-only local Session endpoint; never exposes fixture or ROS controls."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from entryplug.core.evidence import json_object
from entryplug.harness.session_wire import dispatch_session_request
from hybrid_runtime import open_hybrid_runtime

MAX_REQUEST_BYTES = 16_384
MAX_REQUEST_BYTES = 16_384

async def serve(run_dir: Path, run_id: str, token_file: Path) -> None:
    socket_path = run_dir / "session.sock"
    ready_path = run_dir / "session-ready.json"
    closed = asyncio.Event()
    async with open_hybrid_runtime(run_dir, run_id, token_file) as runtime:
        records: list[dict[str, object]] = []

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                while not reader.at_eof():
                    line = await reader.readline()
                    if not line:
                        break
                    if len(line) > MAX_REQUEST_BYTES:
                        break
                    try:
                        request = json.loads(line)
                        if not isinstance(request, dict):
                            raise ValueError("request must be an object")
                        if request.get("kind") == "close":
                            response: dict[str, object] = {"ok": True, "value": {}}
                            closed.set()
                        else:
                            value = await dispatch_session_request(runtime.session, request)
                            response = {"ok": True, "value": value}
                            if request.get("kind") == "act":
                                records.append({"operation_id": value["operation_id"]})
                    except (KeyError, TypeError, ValueError, RuntimeError) as error:
                        response = {"ok": False, "error": type(error).__name__}
                    writer.write(json.dumps(response, allow_nan=False).encode() + b"\n")
                    await writer.drain()
                    if closed.is_set():
                        break
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_unix_server(handle, path=str(socket_path))
        socket_path.chmod(0o600)
        with ready_path.open("x", encoding="utf-8") as stream:
            json.dump({"runtime_id": run_id, "status": "ready"}, stream)
        try:
            async with server:
                await closed.wait()
        finally:
            operations = [
                json_object(
                    await runtime.session.inspect(record["operation_id"], detail="result"),
                    "operation",
                )
                for record in records
            ]
            with (run_dir / "hybrid.json").open("x", encoding="utf-8") as stream:
                json.dump(
                    {
                        "status": "completed",
                        "transport": (
                            "HA WebSocket -> MQTT -> ROS/MuJoCo -> camera -> native Zenoh"
                        ),
                        "home_assistant_entity": runtime.light.entity_id,
                        "operations": operations,
                        "applied_states": runtime.camera.applied_states,
                        "frame_artifacts": runtime.camera.frame_artifacts,
                    },
                    stream,
                    indent=2,
                    sort_keys=True,
                    allow_nan=False,
                )
                stream.write("\n")
            socket_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--token-file", type=Path, default=Path("/run/secrets/ha-token"))
    args = parser.parse_args()
    asyncio.run(serve(args.run_dir, args.run_id, args.token_file))


if __name__ == "__main__":
    main()
