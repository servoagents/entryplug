"""Reproducible local coordination measurements; no inference or physical network."""

import argparse
import asyncio
import importlib
import json
import os
import platform
import resource
import tempfile
import time
from pathlib import Path

from entryplug_app.demo import SEQUENCE
from entryplug_app.service import ApplicationService
from entryplug_app.workspace import Workspace


def rss():
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1])
    return None


async def measure(seconds):
    imports = {"core": rss()}
    for module in (
        "entryplug_server.app",
        "openai",
        "authlib.integrations.httpx_client",
        "mcp",
        "a2a.client",
        "entryplug_homeassistant.light",
    ):
        importlib.import_module(module)
        imports[module] = rss()
    with tempfile.TemporaryDirectory(prefix="entryplug-measure-") as path:
        start = time.perf_counter()
        service = ApplicationService(Workspace.resolve(path))
        await service.open()
        startup_ms = (time.perf_counter() - start) * 1000
        definition = {
            "name": "Measured watch",
            "instructions": "Watch labelled entries",
            "limits": {"max_turns_per_hour": 3600},
        }
        mission = await service.command("mission.create", definition, "create")
        run = await service.command("mission.start", {"mission_id": mission["id"]}, "start")
        idle_wall, idle_cpu = time.perf_counter(), time.process_time()
        await asyncio.sleep(60)
        idle = {
            "seconds": time.perf_counter() - idle_wall,
            "cpu_seconds": time.process_time() - idle_cpu,
            "model_calls": (await service.store.get("runs", run["id"]))["model_calls"],
        }
        sizes = []
        for index in range(seconds):
            await service.demo_step(SEQUENCE[index % len(SEQUENCE)])
            if index % 60 == 0:
                sizes.append(
                    {
                        "second": index,
                        "rss_kib": rss(),
                        "store_bytes": sum(
                            p.stat().st_size
                            for p in service.workspace.state.glob("missions.sqlite3*")
                        ),
                    }
                )
            await asyncio.sleep(1)
        state = await service.snapshot()
        await service.close()
        return {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "cpu": next(
                (
                    line.split(":", 1)[1].strip()
                    for line in Path("/proc/cpuinfo").read_text().splitlines()
                    if line.startswith("model name")
                ),
                "unknown",
            ),
            "logical_cpus": os.cpu_count(),
            "rss_import_stages_kib": imports,
            "owner_open_ms": startup_ms,
            "idle": idle,
            "source_steps": seconds,
            "samples": sizes,
            "alerts": len(state["alerts"]),
            "model_calls": state["runs"][0]["model_calls"],
            "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "stored_records": {k: len(state[k]) for k in ("runs", "turns", "operations", "alerts")},
            "store_bytes_after_close": sum(
                p.stat().st_size for p in Path(path).rglob("missions.sqlite3*")
            ),
        }


parser = argparse.ArgumentParser()
parser.add_argument("--seconds", type=int, default=600)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
args.output.write_text(json.dumps(asyncio.run(measure(args.seconds)), indent=2) + "\n")
