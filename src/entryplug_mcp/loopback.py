"""Owned loopback server for local MCP qualification clients."""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass
from typing import Any

import uvicorn


@dataclass(slots=True)
class LoopbackServer:
    """An MCP application bound to one preallocated local socket."""

    server: uvicorn.Server
    thread: threading.Thread
    port: int

    def close(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5.0)
        if self.thread.is_alive():
            raise RuntimeError("MCP loopback server did not stop")


def start_loopback_server(app: Any) -> LoopbackServer:
    """Start without a port allocation race and require observed readiness."""

    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = int(listener.getsockname()[1])
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="error",
            lifespan="on",
        )
    )
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [listener]},
        name="entryplug-mcp-live-harbor",
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 5.0
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=5.0)
        listener.close()
        raise RuntimeError("MCP loopback server did not start")
    return LoopbackServer(server, thread, port)
