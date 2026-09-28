#!/usr/bin/python3
"""Launch the existing Harbor world with a fixture-only lamp plugin."""

from __future__ import annotations

from launch import LaunchService

from mirrors_launch import description


def main() -> int:
    service = LaunchService()
    service.include_launch_description(description(plugins_file="lamp_spike_plugins.yaml"))
    return service.run()


if __name__ == "__main__":
    raise SystemExit(main())
