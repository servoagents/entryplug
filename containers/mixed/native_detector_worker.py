#!/usr/bin/python3
"""One named native Zenoh detector process for the mixed panel fixture."""

from __future__ import annotations

import argparse
import json
import signal
import threading
from pathlib import Path

import zenoh

from entryplug_zenoh.detector import NativeDetectorServer


def _config(port: int, router_host: str | None = None) -> zenoh.Config:
    endpoint = f"tcp/{router_host or '127.0.0.1'}:{port}"
    return zenoh.Config.from_json5(
        json.dumps(
            {
                "mode": "client" if router_host else "peer",
                "scouting": {"multicast": {"enabled": False}},
                "connect" if router_host else "listen": {"endpoints": [endpoint]},
            }
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--router-host")
    parser.add_argument("--ready", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    report: dict[str, object] = {"worker_id": args.worker_id, "status": "stopped"}
    try:
        with zenoh.open(_config(args.port, args.router_host)) as session:
            worker = NativeDetectorServer(
                session, run_id=args.run_id, worker_id=args.worker_id, generation=1
            )
            try:
                if args.ready is not None:
                    args.ready.open("x").close()
                print(f"native detector {args.worker_id} ready", flush=True)
                stop.wait()
            finally:
                worker.close()
    except Exception as error:
        report["status"] = "failed"
        report["reason"] = type(error).__name__
    if args.report is not None:
        with args.report.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, sort_keys=True, allow_nan=False)
            stream.write("\n")
    return 0 if report["status"] == "stopped" else 1


if __name__ == "__main__":
    raise SystemExit(main())
