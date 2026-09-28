#!/usr/bin/python3
"""Launch the stationary Borrowed Light panel without robot controllers."""

from __future__ import annotations

from pathlib import Path

from launch import LaunchDescription, LaunchService
from launch.actions import Shutdown
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterFile, ParameterValue

ROOT = Path("/workspace/entryplug/containers/harbor")


def description() -> LaunchDescription:
    robot_description = (ROOT / "panel_robot.urdf").read_text(encoding="utf-8")
    return LaunchDescription(
        [
            Node(
                package="robot_state_publisher",
                executable="robot_state_publisher",
                output="both",
                parameters=[
                    {
                        "robot_description": ParameterValue(robot_description, value_type=str),
                        "use_sim_time": True,
                    }
                ],
            ),
            Node(
                package="mujoco_ros2_control",
                executable="ros2_control_node",
                emulate_tty=True,
                output="both",
                parameters=[
                    {"use_sim_time": True},
                    ParameterFile(str(ROOT / "lamp_spike_plugins.yaml")),
                ],
                on_exit=Shutdown(),
            ),
        ]
    )


def main() -> int:
    service = LaunchService()
    service.include_launch_description(description())
    return service.run()


if __name__ == "__main__":
    raise SystemExit(main())
