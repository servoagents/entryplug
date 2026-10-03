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
    parser.add_argument("--hold-result-number", type=int)
    parser.add_argument("--held-file", type=Path)
    args = parser.parse_args()
    if (args.hold_result_number is None) != (args.held_file is None):
        parser.error("held result requires both its sequence and private evidence file")
    if args.hold_result_number is not None and args.hold_result_number < 1:
        parser.error("held result sequence must be positive")
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    report: dict[str, object] = {"worker_id": args.worker_id, "status": "stopped"}

    class HeldResultServer(NativeDetectorServer):
        """Fixture-only fault after a validated real job, before its native reply."""

        def __init__(self, *positional, **keywords):
            self.job_count = 0
            super().__init__(*positional, **keywords)

        def _detect_query(self, query):
            result = super()._detect_query(query)
            self.job_count += 1
            if self.job_count == args.hold_result_number:
                with args.held_file.open("x") as stream:
                    json.dump(
                        {
                            "phase": "before_native_reply",
                            "job_number": self.job_count,
                            "result_identity": {
                                key: result[key]
                                for key in (
                                    "request_id",
                                    "source_id",
                                    "sample_id",
                                    "input_sha256",
                                    "worker_id",
                                    "worker_generation",
                                    "program_id",
                                )
                            },
                        },
                        stream,
                    )
                # The supervisor kills this owned process while its query is active.
                # Bounded fallback keeps a failed injector from wedging teardown.
                stop.wait(10)
                raise RuntimeError("held fixture result was not delivered")
            return result

    try:
        with zenoh.open(_config(args.port, args.router_host)) as session:
            server_type = (
                HeldResultServer if args.hold_result_number is not None else NativeDetectorServer
            )
            worker = server_type(
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
