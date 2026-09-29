#!/usr/bin/env python3
"""One explicit native-compute Zenoh router, separate from ROS RMW traffic."""

from __future__ import annotations

import json
import signal
import threading

import zenoh


def main() -> None:
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    config = zenoh.Config.from_json5(
        json.dumps(
            {
                "mode": "router",
                "scouting": {"multicast": {"enabled": False}},
                "listen": {"endpoints": ["tcp/0.0.0.0:7448"]},
            }
        )
    )
    with zenoh.open(config):
        print("native compute router ready", flush=True)
        stop.wait()


if __name__ == "__main__":
    main()
