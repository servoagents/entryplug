"""Measure the actual foreground HTTP owner with one idle SSE observer."""

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from entryplug_server.client import EntryplugClient


async def measure():
    with tempfile.TemporaryDirectory(prefix="entryplug-http-measure-") as directory:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        start = time.perf_counter()
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "entryplug",
                "serve",
                "--workspace",
                directory,
                "--port",
                str(port),
            ],
            stderr=subprocess.DEVNULL,
        )
        try:
            async with asyncio.timeout(10):
                while not (Path(directory) / "state/service.json").exists():
                    await asyncio.sleep(0.01)
                async with EntryplugClient.from_workspace(directory) as client:
                    while True:
                        try:
                            await client.request("GET", "health")
                            break
                        except Exception:
                            await asyncio.sleep(0.01)
                    startup_ms = (time.perf_counter() - start) * 1000
            async with EntryplugClient.from_workspace(directory) as client:
                mission = await client.create(
                    {"name": "HTTP idle", "instructions": "Wait for events"}
                )
                run = await client.start(mission["mission_id"])
                snapshot = await client.snapshot()

                def sample():
                    fields = Path(f"/proc/{process.pid}/stat").read_text().split()
                    cpu = (int(fields[13]) + int(fields[14])) / os.sysconf("SC_CLK_TCK")
                    rss = next(
                        int(line.split()[1])
                        for line in Path(f"/proc/{process.pid}/status").read_text().splitlines()
                        if line.startswith("VmRSS:")
                    )
                    return cpu, rss

                async with client.http.stream(
                    "GET", "/v1/events", params={"after": snapshot["cursor"]}, timeout=None
                ) as stream:
                    assert stream.status_code == 200
                    before, _ = sample()
                    start = time.perf_counter()
                    await asyncio.sleep(60)
                    elapsed = time.perf_counter() - start
                    after, memory = sample()
                state = await client.request("GET", "runs/" + run["run_id"])
                return {
                    "startup_to_authenticated_http_ms": startup_ms,
                    "idle_seconds": elapsed,
                    "sse_observers": 1,
                    "cpu_seconds": after - before,
                    "percent_of_one_cpu": (after - before) / elapsed * 100,
                    "rss_kib": memory,
                    "model_calls": state["model_calls"],
                }
        finally:
            process.terminate()
            await asyncio.to_thread(process.wait, 10)


print(json.dumps(asyncio.run(measure()), indent=2))
