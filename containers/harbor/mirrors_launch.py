#!/usr/bin/python3
"""Launch the two-camera Hall of Mirrors fixture without a ROS workspace overlay."""

from __future__ import annotations

from pathlib import Path

from launch import LaunchDescription, LaunchService
from launch.actions import Shutdown
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterFile, ParameterValue

ROOT = Path("/workspace/entryplug/containers/harbor")


def description() -> LaunchDescription:
    robot_description = (ROOT / "mirrors_robot.urdf").read_text(encoding="utf-8")
    controllers = str(ROOT / "mirrors_controllers.yaml")
    plugins = str(ROOT / "mirrors_plugins.yaml")
    parameters = [
        {"use_sim_time": True},
        ParameterFile(controllers),
        ParameterFile(plugins),
    ]
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
                parameters=parameters,
                on_exit=Shutdown(),
            ),
            Node(
                package="controller_manager",
                executable="spawner",
                arguments=["joint_state_broadcaster", "--param-file", controllers],
                output="both",
            ),
            Node(
                package="controller_manager",
                executable="spawner",
                arguments=["position_controller", "--param-file", controllers],
                output="both",
            ),
            Node(
                package="controller_manager",
                executable="spawner",
                arguments=["mirror_controller", "--param-file", controllers],
                output="both",
            ),
        ]
    )


def main() -> int:
    service = LaunchService()
    service.include_launch_description(description())
    return service.run()


if __name__ == "__main__":
    raise SystemExit(main())
